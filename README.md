# dotbot-pip

A [Dotbot](https://github.com/anishathalye/dotbot) plugin for installing Python packages with
`pip` and applications with [pipx](https://pipx.pypa.io), including the packages injected into
them. It works like [dotbot-macports](https://github.com/andrewmiskell/dotbot-macports) and
replaces [dotbot-pip](https://github.com/sobolevn/dotbot-pip), which only reads requirements
files and cannot inject packages. It adds these directives:

| Directive | What it does                                                                |
| --------- | --------------------------------------------------------------------------- |
| `pip`     | Installs packages with `pip`                                                 |
| `pipx`    | Installs applications with `pipx`, and injects packages (`ansible +ansible-lint`) |

`pipsi` is not supported: it is no longer maintained.

## Features

- **Lists of packages.** Put the packages in the config, or in text files, one per line.
- **Injection syntax.** `ansible +ansible-lint +sshpass` installs `ansible`, then injects
  `ansible-lint` and `sshpass` into its environment.
- **Fast checks for installed packages.** The plugin calls `pip list` or `pipx list --json`
  once and reuses the result, so anything already installed is skipped right away. Only the
  injections that are missing are injected.
- **Version-aware.** `ansible>=11` and `'+ansible-lint>=25'` install again if the installed
  version doesn't match. Without the `packaging` module only `==` pins are checked.
- **Supports dry runs** (`dotbot --dry-run`).

## Installation

Add the plugin as a submodule of your dotfiles repository:

```shell
git submodule add https://github.com/andrewmiskell/dotbot-pip.git
```

Then load it from your `install` script:

```shell
"${BASEDIR}/${DOTBOT_DIR}/${DOTBOT_BIN}" -d "${BASEDIR}" --plugin-dir dotbot-pip -c "${CONFIG}" "${@}"
```

You can also use `--plugin dotbot-pip/dotbot_pip.py`. Don't load this together with
sobolevn's dotbot-pip: they both define `pip` and `pipx`.

## Migrating from sobolevn/dotbot-pip

This plugin is meant as a drop-in replacement for [sobolevn/dotbot-pip](https://github.com/sobolevn/dotbot-pip),
but the configuration is different, so update it as well as the submodule.

1. **Remove the old plugin.** Both define `pip` and `pipx`, so only one can be loaded.

   ```shell
   git submodule deinit -f dotbot-pip
   git rm -f dotbot-pip
   rm -rf .git/modules/dotbot-pip
   ```

2. **Add this plugin** and keep your `install` script's `--plugin-dir dotbot-pip` (see
   [Installation](#installation)). It uses the same directory name, so the script needs no change.

3. **Update your config.**

   | sobolevn/dotbot-pip                     | This plugin                                             |
   | --------------------------------------- | ------------------------------------------------------- |
   | `file: requirements.txt`                | `files: [requirements.txt]`                             |
   | `binary: /usr/local/bin/pip3` (`pip`)   | `python: /usr/local/bin/python3` (runs `<python> -m pip`) |
   | `binary: /path/to/pipx` (`pipx`)        | `pipx: /path/to/pipx`                                   |
   | `user: true`                            | `user: true` (unchanged)                                |
   | `stderr` defaults to `false`            | `stderr` defaults to `true`                             |
   | `pipsi:`                                | Not supported: `pipsi` is no longer maintained. Use `pipx`. |

   Before:

   ```yaml
   - pip:
       file: requirements.txt
       binary: /usr/local/bin/pip3
       user: true

   - pipx:
       file: requirements-pipx.txt
   ```

   After:

   ```yaml
   - pip:
       files: [requirements.txt]
       python: /usr/local/bin/python3
       user: true

   - pipx:
       files: [requirements-pipx.txt]
   ```

   The `file` key is not recognised, and is ignored without an error. Rename it to `files`, or
   the directive will find nothing to install.

4. **Check your requirements files.** The old plugin ran `pip install -r <file>`. This plugin
   reads each line as its own requirement and installs them one at a time, so a failure doesn't
   stop the rest. Lines starting with `-` (`-r`, `-e`, `--hash`, ...) are skipped with a warning,
   so move those into the `pip` list in your config, or expand them into plain requirements.
   `pipx` files work as before, and can now also use `+` to inject packages.

5. **Optional:** use the new features. `ansible +ansible-lint` injects packages, and
   `include-apps: true` puts their commands on your `PATH`.

Run `dotbot --dry-run` once to confirm that everything is picked up.

## Usage

```yaml
- defaults:
    pipx:
      include-apps: true

- pip:
    - requests
    - rich>=13

- pipx:
    - ansible +ansible-lint +sshpass
    - ruff
    - poetry --python python3.12 +poetry-plugin-export
    - ansible-core==2.16.* --suffix @2-16

- pipx:
    files:
      - pipx-tools.txt
```

### `pip`

A list of requirements, the same as you would give `pip install`. Each one is installed on its
own, so a failure doesn't stop the rest. A requirement with a name (`rich`, `rich>=13`,
`foo @ https://...`) is skipped if a matching version is installed. A path or URL without a
name is always passed to `pip`.

### `pipx`

A list of specs. Each spec is a package, then any number of injections, then any other
`pipx install` arguments:

```
ansible +ansible-lint +sshpass --python python3.12
```

- A token that starts with `+` is injected into the package's environment with `pipx inject`.
  It can be any pip requirement, for example `'+ansible-lint>=25'`.
- The first token is the package (`ansible`, `ansible>=11`, `ansible-core @ https://...`).
  Injecting needs a name, so a bare path or URL can't have injections.
- Everything else is passed to `pipx install` (`--python`, `--include-deps`, `--suffix`, ...).
  With `--suffix`, injections go into the suffixed environment.
- If the package is installed at a version that doesn't match, it is reinstalled with
  `--force` (pipx removes the old environment), and every injection is injected again.
- If the install fails, its injections are skipped and the next spec is tried.

### Files

Give either directive a mapping with `packages` and/or `files` to read specs from text files,
with paths relative to your dotfiles directory. Each line is one spec. Blank lines and
anything after `#` are ignored.

```yaml
- pipx:
    files: [pipx-tools.txt]
    packages:
      - ruff
```

A pip requirements file works as is, but lines starting with `-` (`-r`, `--hash`, ...) are
skipped with a warning.

## Options

Set options for each directive under `defaults`, or inline in the mapping form.

| Option                  | Directive | Default   | Meaning                                                    |
| ----------------------- | --------- | --------- | ---------------------------------------------------------- |
| `python`                | `pip`     | `python3` | Interpreter; pip is run as `<python> -m pip`               |
| `user`                  | `pip`     | `false`   | Pass `--user`                                              |
| `break-system-packages` | `pip`     | `false`   | Pass `--break-system-packages` (for externally managed Pythons) |
| `pipx`                  | `pipx`    | `pipx`    | The `pipx` executable                                      |
| `include-apps`          | `pipx`    | `false`   | Pass `--include-apps` to `pipx inject`, so the injected packages' commands (like `ansible-lint`) are put on your `PATH` |
| `stdin`                 | both      | `true`    | Connect stdin                                              |
| `stdout`                | both      | `false`¹  | Show command output                                        |
| `stderr`                | both      | `true`    | Show command errors                                        |

¹ Running dotbot with `-vv` shows all output.

The plugin doesn't install pip or pipx. Install `pipx` first (for example with a `port` entry
from dotbot-macports) and list that directive before `pipx`. Without it, the plugin reports
that pipx isn't installed.

## Testing

```shell
pip install dotbot pytest pyyaml packaging
pytest
```

The tests use fake `python3` and `pipx` scripts, so they don't need pip, pipx or network
access.
