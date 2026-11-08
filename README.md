# Zammad Branding Remover

A solution that (re-)applies your own branding to a self-hosted [Zammad](https://zammad.com) after every update. It replaces Zammad's hardcoded logos with your own and removes the self-promotion that ships on customer-facing screens.

Built for Zammad installed from the official Debian/Ubuntu package (`/opt/zammad`) and tested against Zammad 7.2.2, but it should also work with newer versions. Zammad package updates overwrite the application files, so the changes have to be re-applied after each update.

## Why?

One of the universal laws of the universe is that all ticket systems suck. Zammad is one whose shortcomings are more or less tolerable. However, out of the box, Zammad shows customers things that have nothing to do with the company they are talking to. In particular, these things make it unprofessional:

- a "Powered by Zammad" footer, with the Zammad logo, on the login, signup and password-reset screens
- a "Did you know? You can help translating Zammad" advertisement on the user's language settings (i.e. your customers see this)
- an ASCII-art banner with a job advert printed to the browser console of every visitor
- the Zammad logo and mascot in the favicon, the home-screen icons, the icon sprite, the error pages and the avatar of the system user
- an unfunny joke about "the Phoenix" on the 404 page

None of these are bugs, but they are also not what a company wants on a customer support portal that carries its own name. From what I understand, you can't even get rid of them if you use Zammad's commercial hosting offers.

The usual way to deal with these things is to find and patch the files by hand after each update. Given that they release updated Debian packages every 2–3 days, that's a lot of manual patching. This repository is an attempt to do it all in a way that is repeatable and reliable.

## Fuck you, this is _wrong_

This is where it gets into [Jamie Zawinski "I would like Debian to stop shipping XScreensaver"](https://www.jwz.org/blog/2016/04/i-would-like-debian-to-stop-shipping-xscreensaver/) territory; the *well if it's legal to do something, then it must also be right to do it* argument.

As of this writing, Zammad is free software, licensed under the AGPL-3.0. Its licence gives every user the freedom to study and modify the program for their own use, and the licence text contains no additional term requiring the "Powered by" footer or the logo to stay, and so altering the branding is within your rights.

So it's legal. But is it right?

You have to answer that yourself.

If feel bad for the Zammad project, give them money. They really like money.

In my own experience, the way Zammad interacts with the community left me less inclined to keep advertising for them. On the community forum, reports from self-hosters mostly get ignored or get a short reply from a guy called MrGeneration whose main contribution seems to be telling people to fuck off.

This script is just a tool. If you don't like it, don't use it. If it gets too popular, we'll see how Zammad's people will start to retaliate. I assume legal harassment and code obfuscation.

## What gets changed

| Area | Change |
| --- | --- |
| "Powered by" blocks | Removed from all legacy `.eco` login/signup/password views and from the Vue desktop and mobile apps |
| Translators-wanted ads | Removed from the user language settings (legacy, desktop and mobile) and from the admin translation page |
| Console banner | `hello_banner.coffee` replaced by a stub that prints nothing |
| Logos | Every stand-alone logo SVG, and the logo symbols in the icon sprite, are regenerated from `data/logo-wordmark.svg` |
| Raster icons | `favicon.ico`, `apple-touch-icon.png` and the PWA `app-icon-*.png` are rendered from your logo |
| Error pages | The artwork in `public/assets/error/*.svg` is replaced by a generic "Error NNN" card with your logo; the "Phoenix" sentence on the 404 pages is reworded. Zammad's own error-page HTML is kept, so tracebacks and updates still work |
| Misc illustrations | Zammad-coloured illustrations (such as the BETA UI dialog banner) are replaced by your logo on a white card |

## What does not get changed

| Area | Reason |
| --- | --- |
| Copyright notices | License violation and no reason to change them |
| License headers | License violation and no reason to change them |
| Logo shown in the app itself | Can be changed in Zammad's admin settings |
 
## Prerequisites

On the Zammad host:

- Zammad installed from the official package, in `/opt/zammad`, with the `zammad` command available (override the location with `ZAMMAD_ROOT` if needed)
- run as **root**, because YOLO
- `bash` 4.4 or newer, `python3` (standard library only), `curl`, `flock`, `systemd`
- `rsvg-convert`, used to render the favicon and app icons: `apt install librsvg2-bin` (on a typical Zammad host this pulls in a single package; the libraries are already installed)
- about 2 GB of free disk space and enough RAM headroom for Zammad's frontend build (the script warns if less than about 1.5 GB is available)

Plan a short maintenance window for a real run. The legacy asset bundle is unavailable while it is recompiled, the Vue build is heavy, and Zammad has to be restarted at the end. On a shit-tier Hetzner Cloud VM, it takes about 10 minutes. Zammad looks broken to visitors while changes are being applied.

## Usage

1. Put everything in a directory on the Zammad host, for example `/opt/fix_zammad_after_update`.
2. Replace `data/logo-wordmark.svg` and optionally add `data/logo-mark.svg` (see further below).
3. Preview what would happen:
   ```bash
   ./removebranding.sh --check
   ```

   It should finish with `0 failure(s)`. Warnings are informational.
5. Take a VM snapshot or backup first if you want a fallback beyond the script's own rollback.
6. Run it:
   ```bash
   ./removebranding.sh
   ```

   This patches the sources, rebuilds the compiled assets (legacy bundle and Vue/vite build), restarts Zammad, and waits until it comes back up.
7. Hard-refresh your browser (Ctrl+F5). `icons.svg` and the favicon are cached aggressively.

### Options

| Option | Effect |
| --- | --- |
| `--check` | Plan, validate and audit only. Nothing is written |
| `--no-rebuild` | Patch the sources only; no asset rebuild, no restart |
| `--no-restart` | Patch and rebuild, but do not restart Zammad |
| `--partial` | Apply what can be patched even if some rule fails (default: refuse and change nothing) |
| `--rollback DIR` | Undo a previous run using its backup directory |

Environment variables: `ZAMMAD_ROOT` (default `/opt/zammad`), `BACKUP_ROOT` (default `/var/backups/zammad-branding`), `KEEP_BACKUPS` (default `5`).

### How to put in your logo

`data/logo-wordmark.svg` is the single source for the sprite symbols, all logo files, the icons and the error art. Replace the AI slop example logo with your own.

Some tips:
- Use plain shapes (`<path>`, `<rect>`, ...) and always convert text to outlines.
- Give it a `viewBox` and make sure the first `<path>` has a reasonably long `d` attribute (the script uses the start of it to recognize its own output)
- A wide logo works best. For tiny square slots (favicon, avatar, sidebar icon) the wide logo is centered on a square canvas.
- For better results add an optional square mark as `data/logo-mark.svg` (same rules); it is then used for those slots automatically

## When Zammad breaks this

A rule that can no longer find its anchor gets reported as `[FAIL]` with the file name and the run aborts without changing anything. Fix or extend the matching rule in `data/patch_branding.py` (the `RULES` table and the `plan_*` functions). The output of `--check` should tell you what is broken.

If you can spare a dollar or two of AI credits, your favorite agent can probably find what's wrong and fix it for you. If you could contribute your fix as a PR, that would help everyone else. 

## License

```
   DO WHAT THE FUCK YOU WANT TO PUBLIC LICENSE
                   Version 2, December 2004
 
Copyright (C) 2004 Sam Hocevar <sam@hocevar.net>

Everyone is permitted to copy and distribute verbatim or modified
copies of this license document, and changing it is allowed as long
as the name is changed.
 
           DO WHAT THE FUCK YOU WANT TO PUBLIC LICENSE
  TERMS AND CONDITIONS FOR COPYING, DISTRIBUTION AND MODIFICATION

 0. You just DO WHAT THE FUCK YOU WANT TO.
```
