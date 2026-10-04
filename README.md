<!--
Copyright (c) 2026 Robert August Vincent II <pillarsdotnet@gmail.com>
Co-author: Claude Code.
-->

# camera-sign

## Background

I work from home and live with a disabled and self-conscious wife. I got tired
of her asking me, with an anxious voice, "Is the camera on?" And she got very
upset the one time she forgot to ask and I told her, after she had walked
across my background, "By the way, the camera was on just now." Hence this app.

## Functionality

Light an "ON AIR" sign somewhere in the house whenever the webcam is live, so
the people at home know not to walk in.

It works with any program that uses the camera -- `Teams` in a browser,
`Zoom`, `OBS` -- because it watches the camera itself rather than any one
app. When anything opens the camera, a `Kasa` smart plug switches on and powers
the sign. When the camera has been closed for a few seconds, the plug switches
off.

It can watch an Android phone's cameras as well, so a video call taken on
the phone lights the sign just as one on the laptop does. The phone is
reached over `adb`, by USB cable or over Wi-Fi; see
[Watching a phone too](#watching-a-phone-too).

## Hardware

| Part                                                                            | Role                      | Price |
| ------------------------------------------------------------------------------- | ------------------------- | ----- |
| [TP-Link `Kasa` EP10 smart plug](https://www.amazon.com/dp/B091699Z3W)          | Switches the sign's power | $10   |
| [`Cjgyz` 8.5 × 11 tabletop LED light box](https://www.amazon.com/dp/B0H17GX6XJ) | The sign                  | $26   |
| [Inkjet transparency film, US Letter](https://www.amazon.com/dp/B0GFT4WND1)     | The "ON AIR" insert       | $7    |

The sign has to light as soon as it gets power. Before relying on it, plug
it into the EP10, turn it on at its own switch, then turn the plug off and on
from the `Kasa` app. If the sign stays dark after the plug comes back on, it
needs a press of its button after every power cut and cannot be used this
way. The one I bought works fine.

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

### On a phone

Android keeps a list of the apps holding each camera open, and
`adb shell dumpsys media.camera` prints it under `Active Camera Clients`:
`[]` when nothing has a camera, or one line per app -- the camera app, a
video call, anything. The watcher reads that list once a second, but only
while the webcam is idle, since either one is enough to light the sign.
Each check takes about a fifth of a second, over USB or Wi-Fi.

A phone that does not answer within 3 seconds -- out of the house, asleep
off the network, unplugged -- counts as idle, so the webcam goes on working
alone. The journal says so once, not every second, and again when the phone
comes back. The `adb` server forgets a Wi-Fi phone when the network drops or
the server restarts, so while one is not answering the watcher runs
`adb connect` again every 30 seconds, without holding up the webcam check.

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

## Watching a phone too

This needs `adb` (`sudo apt install adb`) and a phone running Android 11
or later.

1. Turn on USB debugging on the phone: Settings → About phone → tap Build
   number seven times, then Settings → System → Developer options → USB
   debugging.

2. Plug the phone in, unlocked. A Pixel's USB mode defaults to No data, and
   `adb` cannot see it until that changes: open Settings → Connected
   devices → USB and choose File transfer. Accept the "Allow USB
   debugging?" prompt, ticking Always allow, and `adb devices` lists the
   phone as `device`.

   If `lsusb` shows no Google device at all, the phone is not making a data
   connection. Try the other way up at the phone end, clear lint out of
   its port with a wooden toothpick, or try another cable; some cables
   only carry power.

3. Move it to Wi-Fi, so the phone can be unplugged. Give it a fixed address
   with a DHCP reservation, as for the plug, then:

   ```sh
   adb tcpip 5555
   adb connect 192.168.1.51:5555
   ```

   The phone stops listening on Wi-Fi when it restarts; plug it in and run
   `adb tcpip 5555` again.

4. Add the phone to `~/.config/camera-sign/env`, check it, and restart the
   service:

   ```sh
   echo 'CAMERA_SIGN_PHONE=192.168.1.51:5555' >> ~/.config/camera-sign/env
   camera-sign status --phone 192.168.1.51:5555
   systemctl --user restart camera-sign
   ```

   Use the serial from `adb devices` instead to stay on the cable. The
   journal reports `watching the cameras on phone ...` at startup; open the
   phone's camera, and the sign lights two seconds later.

To stop the phone lighting the sign for a while -- say, a video call on it
from another room -- switch the phone off, and back on afterwards:

```sh
camera-sign phone off
camera-sign phone on
camera-sign phone        # say which it is
```

The running service notices within a second, with no restart, and logs the
change. The setting is a file, `~/.local/state/camera-sign/phone-off`, so it
lasts through restarts and reboots until switched back on. The webcam is
watched either way.

## Usage

```text
camera-sign [watch]   keep the sign in step with the camera (the default)
camera-sign on|off    switch the sign once
camera-sign status    say whether the camera is in use
camera-sign phone [on|off]
                      watch the phone's cameras or not, or say which
```

Add `--phone SERIAL` (or set `CAMERA_SIGN_PHONE`) to watch a phone's cameras
too, with `watch` or `status`. Run `camera-sign --help` for the timing
options.

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
