import json
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

PLUGIN = Path(__file__).resolve().parent.parent / "dotbot_pip.py"

# Fake `python3`: only understands `-m pip list|install`.
FAKE_PYTHON = textwrap.dedent(
    """\
    #!/bin/sh
    dir=$(dirname "$0")
    shift 2   # -m pip
    echo "$*" >> "$dir/calls.log"
    case "$1" in
      list) cat "$dir/pip-list.json" 2>/dev/null || echo '[]' ;;
      install) for a in "$@"; do [ "$a" = broken ] && exit 1; done ;;
    esac
    exit 0
    """
)

# Fake `pipx`: `list --json` prints state.json; install/inject are logged.
FAKE_PIPX = textwrap.dedent(
    """\
    #!/bin/sh
    dir=$(dirname "$0")
    echo "$*" >> "$dir/calls.log"
    case "$1" in
      list) cat "$dir/state.json" 2>/dev/null || echo '{"venvs": {}}'
            [ -f "$dir/list-stderr" ] && cat "$dir/list-stderr" >&2
            exit $(cat "$dir/list-rc" 2>/dev/null || echo 0) ;;
      install) for a in "$@"; do [ "$a" = broken ] && exit 1; done
               [ -f "$dir/install-writes" ] && cp "$dir/install-writes" "$dir/state.json" ;;
      inject) for a in "$@"; do [ "$a" = broken ] && exit 1; done ;;
    esac
    exit 0
    """
)


def venv(version, injected=None):
    return {
        "metadata": {
            "main_package": {"package_version": version},
            "injected_packages": {n: {"package_version": v} for n, v in (injected or {}).items()},
        }
    }


@pytest.fixture
def env(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in (("python3", FAKE_PYTHON), ("pipx", FAKE_PIPX)):
        f = bindir / name
        f.write_text(body)
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
    base = tmp_path / "dotfiles"
    base.mkdir()

    class Env:
        def __init__(self):
            self.bindir = bindir
            self.base = base

        def pip_installed(self, **pkgs):
            (bindir / "pip-list.json").write_text(json.dumps([{"name": n, "version": v} for n, v in pkgs.items()]))

        def pipx_installed(self, **venvs):
            (bindir / "state.json").write_text(json.dumps({"venvs": venvs}))

        def calls(self):
            log = bindir / "calls.log"
            lines = log.read_text().splitlines() if log.exists() else []
            return [c for c in lines if c not in ("list --format=json", "list --json")]

        def run(self, config, *args):
            defaults = {
                "pip": {"python": str(bindir / "python3")},
                "pipx": {"pipx": str(bindir / "pipx")},
            }
            full = [{"defaults": defaults}, *config]
            conf = base / "install.conf.yaml"
            conf.write_text(yaml.safe_dump(full))
            return subprocess.run(
                [str(Path(sys.executable).parent / "dotbot"), "-d", str(base), "-c", str(conf), "--plugin", str(PLUGIN), *args],
                capture_output=True,
                text=True,
                check=False,
            )

    return Env()


def out(r):
    return r.stdout + r.stderr


# ---------------------------------------------------------------------- pip


def test_pip_installs_missing_only(env):
    env.pip_installed(requests="2.32.0", PyYAML="6.0")
    r = env.run([{"pip": ["requests", "pyyaml", "rich"]}])
    assert r.returncode == 0, out(r)
    assert env.calls() == ["install rich"]


def test_pip_normalizes_names(env):
    env.pip_installed(**{"typing_extensions": "4.12.0"})
    r = env.run([{"pip": ["typing-extensions"]}])
    assert r.returncode == 0, out(r)
    assert env.calls() == []


def test_pip_version_specifiers(env):
    env.pip_installed(requests="2.31.0", rich="13.0.0", black="24.1.0")
    r = env.run([{"pip": ["requests>=2.32", "rich==13.0.0", "black ~= 24.0", "black==25.*"]}])
    assert r.returncode == 0, out(r)
    assert env.calls() == ["install requests>=2.32", "install black==25.*"]


def test_pip_flags(env):
    r = env.run([{"pip": {"packages": ["rich"], "user": True, "break-system-packages": True}}])
    assert r.returncode == 0, out(r)
    assert env.calls() == ["install --user --break-system-packages rich"]


def test_pip_failure_continues(env):
    r = env.run([{"pip": ["broken", "rich"]}])
    assert r.returncode != 0
    assert env.calls() == ["install broken", "install rich"]
    assert "Failed to install [broken]" in out(r)


def test_pip_files(env):
    env.pip_installed(requests="2.32.0")
    (env.base / "requirements.txt").write_text("# tools\nrequests\n\nrich  # pretty\n-r other.txt\n")
    r = env.run([{"pip": {"files": "requirements.txt"}}])
    assert r.returncode == 0, out(r)
    assert env.calls() == ["install rich"]
    assert "Skipping unsupported option line" in out(r)


def test_pip_url_always_installs(env):
    r = env.run([{"pip": ["git+https://example.com/x.git", "foo @ https://example.com/foo.whl"]}])
    assert r.returncode == 0, out(r)
    assert env.calls() == ["install git+https://example.com/x.git", "install foo @ https://example.com/foo.whl"]


# --------------------------------------------------------------------- pipx


def test_pipx_install_with_injections(env):
    r = env.run([{"pipx": ["ansible +ansible-lint +sshpass", "ruff"]}])
    assert r.returncode == 0, out(r)
    assert env.calls() == ["install ansible", "inject ansible ansible-lint sshpass", "install ruff"]


def test_pipx_skips_installed(env):
    env.pipx_installed(ansible=venv("11.0.0", {"ansible-lint": "25.1.0", "sshpass": "1.0"}), ruff=venv("0.8.0"))
    r = env.run([{"pipx": ["ansible +ansible-lint +sshpass", "ruff"]}])
    assert r.returncode == 0, out(r)
    assert env.calls() == []


def test_pipx_injects_only_missing(env):
    env.pipx_installed(ansible=venv("11.0.0", {"ansible-lint": "25.1.0"}))
    r = env.run([{"pipx": ["ansible +ansible-lint +sshpass"]}])
    assert r.returncode == 0, out(r)
    assert env.calls() == ["inject ansible sshpass"]


def test_pipx_include_apps(env):
    env.pipx_installed(ansible=venv("11.0.0"))
    r = env.run([{"pipx": {"packages": ["ansible +ansible-lint"], "include-apps": True}}])
    assert r.returncode == 0, out(r)
    assert env.calls() == ["inject --include-apps ansible ansible-lint"]


def test_pipx_version_mismatch_forces_reinstall(env):
    env.pipx_installed(ansible=venv("10.0.0", {"ansible-lint": "25.1.0"}))
    r = env.run([{"pipx": ["ansible>=11 +ansible-lint"]}])
    assert r.returncode == 0, out(r)
    assert env.calls() == ["install --force ansible>=11", "inject ansible ansible-lint"]


def test_pipx_injection_version_specifier(env):
    env.pipx_installed(ansible=venv("11.0.0", {"ansible-lint": "24.1.0", "sshpass": "1.0"}))
    r = env.run([{"pipx": ["ansible '+ansible-lint>=25' +sshpass"]}])
    assert r.returncode == 0, out(r)
    assert env.calls() == ["inject ansible ansible-lint>=25"]


def test_pipx_extra_install_args(env):
    r = env.run([{"pipx": ["poetry --python python3.12 +poetry-plugin-export"]}])
    assert r.returncode == 0, out(r)
    assert env.calls() == ["install --python python3.12 poetry", "inject poetry poetry-plugin-export"]


def test_pipx_suffix_uses_suffixed_env(env):
    env.pipx_installed(**{"ansible@2-16": venv("2.16.0")})
    r = env.run([{"pipx": ["ansible==2.16.* --suffix @2-16 +ansible-lint"]}])
    assert r.returncode == 0, out(r)
    assert env.calls() == ["inject ansible@2-16 ansible-lint"]


def test_pipx_failed_install_skips_injection(env):
    r = env.run([{"pipx": ["broken +foo", "ruff"]}])
    assert r.returncode != 0
    assert env.calls() == ["install broken", "install ruff"]
    assert "Failed to install [broken +foo]" in out(r)


def test_pipx_failed_injection_continues(env):
    r = env.run([{"pipx": ["ansible +broken", "ruff"]}])
    assert r.returncode != 0
    assert env.calls() == ["install ansible", "inject ansible broken", "install ruff"]


def test_pipx_url_without_name_cannot_inject(env):
    r = env.run([{"pipx": ["git+https://example.com/x.git +foo"]}])
    assert r.returncode != 0
    assert env.calls() == []
    assert "Cannot inject" in out(r)


def test_pipx_files(env):
    env.pipx_installed(ruff=venv("0.8.0"))
    (env.base / "pipx.txt").write_text("# tools\nruff\nansible +ansible-lint  # config mgmt\n")
    r = env.run([{"pipx": {"files": ["pipx.txt"]}}])
    assert r.returncode == 0, out(r)
    assert env.calls() == ["install ansible", "inject ansible ansible-lint"]


# ------------------------------------------------------------------- common


def test_dry_run_runs_nothing(env):
    env.pipx_installed(ansible=venv("11.0.0"))
    r = env.run(
        [{"pip": ["rich"]}, {"pipx": ["ansible +ansible-lint", "ruff +foo"]}],
        "--dry-run",
    )
    assert r.returncode == 0, out(r)
    assert env.calls() == []
    for msg in ("Would install rich", "Would inject ansible-lint into ansible", "Would install ruff"):
        assert msg in out(r)


def test_missing_pipx_errors(env):
    (env.bindir / "pipx").unlink()
    r = env.run([{"pipx": ["ruff"]}])
    assert r.returncode != 0
    assert "pipx` was not found" in out(r)


def test_pipx_list_nonzero_with_json_warns_and_continues(env):
    # pipx exits 1 (but still prints JSON) when an environment's Python is missing
    env.pipx_installed(ruff=venv("0.8.0"))
    (env.bindir / "list-rc").write_text("1")
    (env.bindir / "install-writes").write_text(
        json.dumps({"venvs": {"ruff": venv("0.8.0"), "yamllint": venv("1.35.0")}})
    )
    r = env.run([{"pipx": ["ruff", "yamllint"]}])
    assert r.returncode == 0, out(r)
    assert env.calls() == ["install yamllint"]
    assert "pipx reinstall-all" in out(r)


def test_pipx_broken_env_install_is_noop_skips_injection(env):
    # `pipx list` leaves out an env whose Python is missing, and `pipx install`
    # then exits 0 without doing anything; injecting would crash pipx
    env.pipx_installed()
    (env.bindir / "list-rc").write_text("1")
    r = env.run([{"pipx": ["ansible +ansible-lint", "ruff"]}])
    assert r.returncode != 0
    assert env.calls() == ["install ansible", "install ruff"]
    assert "environment exists but is broken" in out(r)
    assert "pipx reinstall ansible" in out(r)


def test_pipx_degraded_list_still_injects_after_real_install(env):
    env.pipx_installed()
    (env.bindir / "list-rc").write_text("1")
    (env.bindir / "install-writes").write_text(json.dumps({"venvs": {"ansible": venv("11.0.0")}}))
    r = env.run([{"pipx": ["ansible +ansible-lint"]}])
    assert r.returncode == 0, out(r)
    assert env.calls() == ["install ansible", "inject ansible ansible-lint"]
    assert out(r).count("pipx reinstall-all") == 1  # warned once, not per spec


def test_pipx_list_failure_reports_status_and_stderr(env):
    (env.bindir / "state.json").write_text("not json")
    (env.bindir / "list-rc").write_text("2")
    (env.bindir / "list-stderr").write_text("something exploded\n")
    r = env.run([{"pipx": ["ruff"]}])
    assert r.returncode != 0
    assert env.calls() == []
    assert "failed with exit status 2: something exploded" in out(r)
    assert "was not found" not in out(r)


def test_bad_data_errors(env):
    r = env.run([{"pipx": "ruff"}])
    assert r.returncode != 0
    assert "expects a list" in out(r)
