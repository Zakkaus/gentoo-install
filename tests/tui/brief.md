<!-- SPDX-License-Identifier: GPL-2.0-or-later -->
# Installing a machine through the interface

You are the operator of a machine that is booted from the installer's medium
and showing its interface. Build the machine described in your spec by using
that interface, and nothing else.

## What you may run

One command, with these subcommands:

```
python3 -m tests.tui.session screen <id>          what is on the screen now
python3 -m tests.tui.session key <id> <keys...>   press keys
python3 -m tests.tui.session plan <id>            what the installer would do
```

Keys are named: `up`, `down`, `left`, `right`, `enter`, `esc`, `tab`, `space`,
`home`, `end`, `pageup`, `pagedown`, `help`, `backspace`, and `type:<text>` to
enter text. Several may be given at once: `key lab1 down down enter`.

## What you may not do

- Do not read `gentoo_install/`, `tests/fixtures/`, or any other file in this
  repository. The question is whether the screen says enough on its own; a
  source file answers it for you and destroys the measurement.
- Do not pass `--config`, edit a configuration file, or start the installer
  yourself. The session is already running.
- Do not run any command other than the three above.
- Do not answer from what a Gentoo install usually needs. If the screen does
  not say which row sets something, that is the finding.

## How to work

Read the screen, decide which row answers the next part of your spec, open it,
set it, and go back. Take the spec in whatever order the interface makes
natural, not the order it is written in. When a screen refuses what you typed,
read what it says and correct it.

When you cannot tell what a row is for, or cannot find the row that sets
something your spec names, press `help` once and read what it says. If that
does not answer it, record the row's name under `unclear` and move on — do not
guess, and do not brute-force the screen.

Stop when the plan the installer would run matches your spec, or when you
cannot make further progress.

## What to answer

One JSON object, and nothing else:

```json
{
  "finished": true,
  "installed": ["the root filesystem is btrfs", "hostname is lab1"],
  "stuck": ["Kernel"],
  "unclear": ["Firmware: could not tell whether it detects or sets"]
}
```

`installed` lists the lines of your spec's proof that the plan shows were
answered. `stuck` names a screen you could not leave or could not set.
`unclear` names a row whose purpose the screen did not convey, one line each,
saying what was missing. Your answer is read as a claim about the interface,
not as the result of the run: the run is counted from the keys you pressed.
