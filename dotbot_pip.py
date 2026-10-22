import json
import os
import re
import shlex
import subprocess
from typing import Any, Callable, Dict, List, Mapping, NamedTuple, Optional, Set, Tuple

import dotbot
from dotbot.util.common import shell_command

try:
    from packaging.specifiers import InvalidSpecifier, SpecifierSet
    from packaging.version import InvalidVersion, Version
except ImportError:  # packaging is optional; without it only `==` pins are checked
    SpecifierSet = None  # type: ignore[assignment,misc]


class Requirement(NamedTuple):
    name: Optional[str]  # None when the spec is a path or URL with no `name @`
    specifier: str


_NAME = r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?"


def normalize(name: str) -> str:
    """PEP 503 name normalization."""
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_requirement(spec: str) -> Requirement:
    """
    Pull the project name and version specifier out of a requirement like
    "ansible-core[foo]>=2.17,<3 ; python_version > '3.9'" or "name @ https://...".
    """
    spec = spec.strip()
    m = re.fullmatch(rf"({_NAME})\s*(?:\[[^\]]*\])?\s*@\s*\S.*", spec)
    if m:
        return Requirement(m[1], "")
    m = re.fullmatch(rf"({_NAME})\s*(?:\[[^\]]*\])?\s*([<>=!~][^;]*)?(?:;.*)?", spec)
    if m:
        return Requirement(m[1], (m[2] or "").strip())
    return Requirement(None, "")


def version_satisfies(version: str, specifier: str) -> bool:
    if not specifier:
        return True
    if SpecifierSet is not None:
        try:
            return SpecifierSet(specifier).contains(Version(version), prereleases=True)
        except (InvalidSpecifier, InvalidVersion):
            return False
    m = re.fullmatch(r"===?\s*(\S+)", specifier)
    if m and "*" not in m[1]:
        return m[1] == version
    return True  # can't evaluate without `packaging`, so trust what is installed


class PythonPackages(dotbot.Plugin):
    """
    Install Python packages with pip, and applications with pipx (including
    the packages injected into them).
    """

    supports_dry_run = True

    _defaults: Mapping[str, Mapping[str, Any]] = {
        "pip": {
            "stdin": True,
            "stdout": False,
            "stderr": True,
            "python": "python3",
            "user": False,
            "break-system-packages": False,
        },
        "pipx": {
            "stdin": True,
            "stdout": False,
            "stderr": True,
            "pipx": "pipx",
            "include-apps": False,
        },
    }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._directives: Dict[str, Callable[[Any, Dict[str, Any]], bool]] = {
            "pip": self._pip,
            "pipx": self._pipx,
        }
        self._pip_cache: Optional[Dict[str, str]] = None
        self._pipx_cache: Optional[Dict[str, Dict[str, Any]]] = None
        self._list_error = ""

    def can_handle(self, directive: str) -> bool:
        return directive in self._directives

    def handle(self, directive: str, data: Any) -> bool:
        if directive not in self._directives:
            msg = f"dotbot-pip cannot handle directive {directive}"
            raise ValueError(msg)
        user_defaults = self._context.defaults().get(directive, {})
        defaults = {**self._defaults[directive], **user_defaults}

        files: List[str] = []
        if isinstance(data, dict):
            defaults = {**defaults, **{k: v for k, v in data.items() if k not in ("packages", "files")}}
            files = data.get("files") or []
            data = data.get("packages") or []
        if isinstance(files, str):
            files = [files]
        if not isinstance(data, list) or not isinstance(files, list):
            self._log.error(f"{directive} expects a list of packages, or a mapping with `packages` and `files`")
            return False
        try:
            specs = [*data, *self._read_files(files)]
        except OSError as e:
            self._log.error(f"Could not read package file: {e}")
            return False
        return self._directives[directive](specs, defaults)

    # ------------------------------------------------------------------ helpers

    def _run(self, cmd: str, defaults: Mapping[str, Any]) -> int:
        stdout = defaults["stdout"]
        stderr = defaults["stderr"]
        if self._context.options().verbose > 1:
            stdout = stderr = True
        return shell_command(
            cmd,
            cwd=self._context.base_directory(),
            enable_stdin=defaults["stdin"],
            enable_stdout=stdout,
            enable_stderr=stderr,
        )

    def _action(self, msg: str) -> None:
        if self._context.dry_run():
            msg = "Would " + msg[0].lower() + msg[1:]
        self._log.action(msg)

    def _read_files(self, paths: List[str]) -> List[str]:
        """
        Read package specs from plain-text files, one per line. Blank lines
        and anything after ` #` (or a leading `#`) are ignored.
        """
        specs: List[str] = []
        for path in paths:
            full = os.path.join(self._context.base_directory(), os.path.expanduser(path))
            with open(full) as f:
                for line in f.read().splitlines():
                    line = re.sub(r"(^|\s)#.*", "", line).strip()
                    if not line:
                        continue
                    if line.startswith("-"):
                        self._log.warning(f"Skipping unsupported option line in {path}: {line}")
                        continue
                    specs.append(line)
        return specs

    @staticmethod
    def _quote(*tokens: str) -> str:
        return " ".join(shlex.quote(t) for t in tokens)

    @staticmethod
    def _query(cmd: List[str]) -> Optional[Tuple[int, str, str]]:
        """Run a command and return (exit status, stdout, stderr); None if it can't be run."""
        try:
            result = subprocess.run(  # noqa: S603
                cmd, capture_output=True, text=True, check=False
            )
        except OSError:
            return None
        return result.returncode, result.stdout, result.stderr.strip()

    @staticmethod
    def _failure(cmd: List[str], res: Optional[Tuple[int, str, str]]) -> str:
        shown = " ".join(cmd)
        if res is None:
            return f"Could not run `{shown}`: `{cmd[0]}` was not found (install it, or set its full path in the options)"
        detail = f": {res[2].splitlines()[-1]}" if res[2] else ""
        return f"`{shown}` failed with exit status {res[0]}{detail}"

    # ---------------------------------------------------------------------- pip

    def _pip_installed(self, defaults: Mapping[str, Any]) -> Optional[Dict[str, str]]:
        """Map each installed distribution to its version, from a single `pip list`."""
        if self._pip_cache is None:
            cmd = [defaults["python"], "-m", "pip", "list", "--format=json"]
            res = self._query(cmd)
            if res is None or res[0] != 0:
                self._list_error = self._failure(cmd, res)
                return None
            try:
                self._pip_cache = {normalize(p["name"]): p["version"] for p in json.loads(res[1])}
            except (ValueError, KeyError, TypeError):
                self._list_error = f"Could not read the output of `{' '.join(cmd)}`"
                return None
        return self._pip_cache

    def _pip(self, specs: List[Any], defaults: Dict[str, Any]) -> bool:
        installed = self._pip_installed(defaults)
        if installed is None:
            if not self._context.dry_run():
                self._log.error(self._list_error)
                return False
            installed = {}

        flags: List[str] = []
        if defaults["user"]:
            flags.append("--user")
        if defaults["break-system-packages"]:
            flags.append("--break-system-packages")

        success = True
        for spec in specs:
            if not isinstance(spec, str) or not spec.strip():
                self._log.error(f"Invalid pip entry: {spec!r}")
                success = False
                continue
            req = parse_requirement(spec)
            have = installed.get(normalize(req.name)) if req.name else None
            if have is not None and version_satisfies(have, req.specifier):
                self._log.debug(f"{spec} is already installed")
                continue

            cmd = self._quote(defaults["python"], "-m", "pip", "install", *flags, spec.strip())
            self._action(f"Install {spec}")
            if self._context.dry_run():
                continue
            if self._run(cmd, defaults) != 0:
                self._log.warning(f"Failed to install [{spec}]")
                success = False
            else:
                self._pip_cache = None

        if success:
            self._log.info("All pip packages have been installed")
        else:
            self._log.error("Some pip packages were not installed")
        return success

    # --------------------------------------------------------------------- pipx

    @staticmethod
    def _parse_pipx_spec(spec: str) -> Tuple[str, List[str], List[str]]:
        """
        Split a spec like "ansible +ansible-lint +sshpass --python python3.12"
        into the package, the packages to inject, and any other `pipx install`
        arguments.
        """
        tokens = shlex.split(spec)
        package = tokens[0]
        injections: List[str] = []
        rest: List[str] = []
        for tok in tokens[1:]:
            if tok.startswith("+") and len(tok) > 1:
                injections.append(tok[1:])
            else:
                rest.append(tok)
        return package, injections, rest

    @staticmethod
    def _venv_name(name: str, rest: List[str]) -> str:
        """The pipx environment for a package; `--suffix` changes it."""
        suffix = ""
        for i, tok in enumerate(rest):
            if tok == "--suffix" and i + 1 < len(rest):
                suffix = rest[i + 1]
            elif tok.startswith("--suffix="):
                suffix = tok.split("=", 1)[1]
        return normalize(name) + suffix.lower()

    def _pipx_installed(self, defaults: Mapping[str, Any]) -> Optional[Dict[str, Dict[str, Any]]]:
        """
        Map each pipx environment to its main package version and injected
        packages (name -> version), from a single `pipx list --json`.
        """
        if self._pipx_cache is None:
            cmd = [defaults["pipx"], "list", "--json"]
            res = self._query(cmd)
            if res is None:
                self._list_error = self._failure(cmd, res)
                return None
            try:
                venvs = json.loads(res[1])["venvs"]
                self._pipx_cache = {
                    normalize(env): {
                        "version": v["metadata"]["main_package"].get("package_version", ""),
                        "injected": {
                            normalize(n): p.get("package_version", "")
                            for n, p in (v["metadata"].get("injected_packages") or {}).items()
                        },
                    }
                    for env, v in venvs.items()
                }
            except (ValueError, KeyError, TypeError, AttributeError):
                self._list_error = (
                    self._failure(cmd, res) if res[0] != 0 else f"Could not read the output of `{' '.join(cmd)}`"
                )
                return None
            if res[0] != 0:
                # pipx still prints the environments it can read, but leaves out
                # (and exits 1 for) any whose Python interpreter has gone missing
                self._log.warning(
                    f"`{' '.join(cmd)}` exited with status {res[0]}, so broken environments are treated "
                    "as not installed. Run `pipx list` to see which, and `pipx reinstall-all` to fix them."
                )
        return self._pipx_cache

    def _pipx(self, specs: List[Any], defaults: Dict[str, Any]) -> bool:
        installed = self._pipx_installed(defaults)
        if installed is None:
            if not self._context.dry_run():
                self._log.error(self._list_error)
                return False
            installed = {}

        success = True
        for spec in specs:
            if not isinstance(spec, str) or not spec.strip():
                self._log.error(f"Invalid pipx entry: {spec!r}")
                success = False
                continue
            try:
                package, injections, rest = self._parse_pipx_spec(spec)
            except ValueError as e:
                self._log.error(f"Invalid pipx entry [{spec}]: {e}")
                success = False
                continue
            if not self._pipx_one(spec, package, injections, rest, installed, defaults):
                success = False
            installed = self._pipx_installed(defaults) or {}

        if success:
            self._log.info("All pipx packages have been installed")
        else:
            self._log.error("Some pipx packages were not installed")
        return success

    def _pipx_one(
        self,
        spec: str,
        package: str,
        injections: List[str],
        rest: List[str],
        installed: Dict[str, Dict[str, Any]],
        defaults: Mapping[str, Any],
    ) -> bool:
        req = parse_requirement(package)
        if req.name is None and injections:
            self._log.error(f"Cannot inject into [{package}]: use `name @ url` so the environment can be found")
            return False
        env = self._venv_name(req.name, rest) if req.name else None
        current = installed.get(env) if env else None
        changed = False

        # (re)install the application unless it is already there at a matching version
        if current is not None and version_satisfies(current["version"], req.specifier):
            have_injected: Set[str] = set(current["injected"])
            versions = current["injected"]
        else:
            args = [*rest]
            if current is not None:
                # wrong version: pipx only replaces an environment with --force
                args.insert(0, "--force")
            cmd = self._quote(defaults["pipx"], "install", *args, package)
            self._action(f"Install {package}")
            if not self._context.dry_run():
                changed = True
                if self._run(cmd, defaults) != 0:
                    self._pipx_cache = None
                    self._log.warning(f"Failed to install [{spec}]")
                    return False
            have_injected, versions = set(), {}

        missing: List[str] = []
        for inj in injections:
            inj_req = parse_requirement(inj)
            have = versions.get(normalize(inj_req.name)) if inj_req.name else None
            if inj_req.name and normalize(inj_req.name) in have_injected and version_satisfies(
                have or "", inj_req.specifier
            ):
                self._log.debug(f"{inj} is already injected into {env}")
                continue
            missing.append(inj)

        if missing:
            flags = ["--include-apps"] if defaults["include-apps"] else []
            cmd = self._quote(defaults["pipx"], "inject", *flags, env or "", *missing)
            self._action(f"Inject {', '.join(missing)} into {env}")
            if not self._context.dry_run():
                changed = True
                if self._run(cmd, defaults) != 0:
                    self._pipx_cache = None
                    self._log.warning(f"Failed to inject {', '.join(missing)} into [{env}]")
                    return False

        if changed:
            self._pipx_cache = None
        return True
