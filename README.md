<!--
Copyright (c) 2026 Robert August Vincent II <pillarsdotnet@gmail.com>
Co-author: Claude Code.
-->

# camera-sign

Light an "ON AIR" sign elsewhere in the house whenever the webcam is live, so
the people at home know not to walk in.

It works with any program that uses the camera -- `Teams` in a browser,
`Zoom`, `OBS` -- because it watches the camera itself rather than any one
app. When anything opens the camera, a `Kasa` smart plug switches on and powers
the sign. When the camera has been closed for a few seconds, the plug switches
off.

## Hardware

| Part                                    | Role                      |
| --------------------------------------- | ------------------------- |
| TP-Link `Kasa` EP10 smart plug          | Switches the sign's power |
| `Cjgyz` 8.5 × 11 tabletop LED light box | The sign                  |
| Inkjet transparency film, US Letter     | The "ON AIR" insert       |

The sign has to light as soon as it gets power. Before relying on it, plug
it into the EP10, turn it on at its own switch, then turn the plug off and on
from the `Kasa` app. If the sign stays dark after the plug comes back on, it
needs a press of its button after every power cut and cannot be used this
way.

### The insert

There are two to choose from: [`print/on-air.svg`](print/on-air.svg), a plain
"ON AIR" printed landscape, and [`print/camera-on.svg`](print/camera-on.svg),
printed portrait, which reads "CAMERA ON (when lit)" and "ENTER AT OWN RISK"
around a laptop on a video call.

Print either one at 100% scale, in its own orientation, on the
rough (coated) side of the film. Let it dry fully before handling it. Inkjet
black lets some light through; if the background glows, print a second copy
and stack the two sheets, lined up, in the frame.

## How it detects the camera

Every process that opens a USB webcam raises the `uvcvideo` driver's
reference count, in `/sys/module/uvcvideo/refcnt`, by one. The watcher reads
that one file every second. It needs no special permissions and sees the
camera no matter which user or program opened it.

Browsers sometimes open the camera for a moment just to list devices, so the
sign lights only after the camera has stayed open for 2 seconds, and goes dark
only after it has stayed closed for 5.

The watcher also switches the sign off when it stops (at logout, for
instance) and just before the laptop suspends or hibernates, so the sign is
never left lit by accident.

## Install

On Ubuntu 24.04 (`noble`), 26.04 (`resolute`) or 26.10 (`stonking`), the
Debian package in
[`ppa:pillarsdotnet/ppa`](https://launchpad.net/~pillarsdotnet/+archive/ubuntu/ppa)
brings the `python3-kasa` it needs from the same PPA too:

```sh
sudo add-apt-repository ppa:pillarsdotnet/ppa
sudo apt install camera-sign
```

It installs the command, the `systemd` user unit (enabled for nobody), the
example settings at `/usr/share/doc/camera-sign/examples/env.example`, and the
printable inserts in `/usr/share/camera-sign/print/`. Then set it up with
`/usr/share/doc/camera-sign/README.Debian`: the same steps as below, but using
the copies the package installed instead of downloading them.

Elsewhere, install it with `uv`:

1. Set up the plug in the `Kasa` app, then give it a fixed address with a DHCP
   reservation on the router. Find its address with `kasa discover`.

2. Install the command:

   ```sh
   uv tool install git+https://github.com/pillarsdotnet/camera-sign
   ```

3. Tell it where the plug is, and check it works:

   ```sh
   mkdir -p ~/.config/camera-sign
   curl -fsSL https://raw.githubusercontent.com/pillarsdotnet/camera-sign/main/systemd/env.example \
     > ~/.config/camera-sign/env
   $EDITOR ~/.config/camera-sign/env
   set -a; . ~/.config/camera-sign/env; set +a
   camera-sign on && sleep 3 && camera-sign off
   ```

   Newer `Kasa` firmware refuses local commands without the TP-Link account
   the plug was set up with. If `camera-sign on` fails with an authentication
   error, set `CAMERA_SIGN_USERNAME` and `CAMERA_SIGN_PASSWORD` in that file.

4. Run it as a service:

   ```sh
   curl -fsSL https://raw.githubusercontent.com/pillarsdotnet/camera-sign/main/systemd/camera-sign.service \
     > ~/.config/systemd/user/camera-sign.service
   systemctl --user daemon-reload
   systemctl --user enable --now camera-sign
   journalctl --user -u camera-sign -f
   ```

## Usage

```text
camera-sign [watch]   keep the sign in step with the camera (the default)
camera-sign on|off    switch the sign once
camera-sign status    say whether the camera is in use
```

Run `camera-sign --help` for the timing options.

## Development

```sh
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
pre-commit install
```

Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/).

## Licence

GNU General Public License, version 3 or (at your option) any later
version; see [`LICENSE`](LICENSE).
