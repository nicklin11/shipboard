# Key relay: hotkeys without the `input` group

The daemon reads its trigger keys straight from `/dev/input`. The usual way to allow
that, adding yourself to the `input` group, also lets **every program you run read
everything you type**, so any malware running as you gets a keylogger for free.

The relay avoids that. It runs as a sandboxed system service (no network, no file
writes, its own throwaway user), reads the keyboards, and re-emits **only your
trigger keys** on a small virtual keyboard called `shipboard-keys`:

```
keyboards ──> shipboard-key-relay (system service, in `input`)
                 │  only the trigger keys, press/release
                 v
              shipboard-keys (virtual) ──> shipboard daemon (you, NOT in `input`)
                                     └──x  compositor (libinput ignores it: no doubled keys)
```

The daemon needs no configuration change: it already picks any readable device
that has a bound key, and the relay's device is the only one it can read.

## Install

```sh
sudo contrib/key-relay/install.sh --keys rightalt          # your [[key_bind]] keys, comma-separated
sudo gpasswd -d "$USER" input                              # if you were in the group
```

Log out and back in (group changes apply to new sessions only). Then
`journalctl -u shipboard-key-relay` should list the keyboards it listens to, and
your trigger key works as before.

Change keys: run `install.sh --keys ...` again. Remove: `sudo contrib/key-relay/install.sh --uninstall`.

## What it can and can't see

- It reads all keyboards, but its code drops every key except the configured ones
  before anything else happens, never logs a key, and the sandbox has no network.
- You (the logged-in user at the seat) can read the virtual device through the
  standard `uaccess` ACL, and you only ever see the trigger keys there.
- Autorepeat is dropped; the same key held on two keyboards is one press.
- New keyboards are picked up within 2 s; an unplugged keyboard releases its keys.
