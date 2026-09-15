# Linux computers

A Linux computer gives an agent a complete visible desktop with Chromium, a terminal, a file manager, and a text editor. Use it when the work spans more than one desktop application or when a person needs to watch and take control.

## Choose the right resource

Use the smallest resource that fits the job:

| Need | Use |
| --- | --- |
| Run commands or code | A [sandbox](sandboxes.md) |
| Automate websites through Chromium | A [browser sandbox](browser.md) |
| Work across a visible desktop and its applications | A Linux computer |

A computer includes a browser, but a browser sandbox is still the simpler choice for web-only work.

SmolVM downloads the Linux desktop image on first use and verifies its checksum. Later starts reuse
the cached image, so applications do not need Docker to create a computer.

## Start and view a computer

Start the built-in Linux desktop template:

```bash
smolvm computer start --name assistant
```

The command prints the computer's viewer URL. Open it later by name:

```bash
smolvm computer open assistant
```

List available templates and active computers:

```bash
smolvm computer templates
smolvm computer list
```

Delete the computer when the work is finished:

```bash
smolvm computer delete assistant
```

Deletion removes the computer and its temporary files.

## Use it from Python

The returned object groups each capability by what it controls. `display` is for people or visual-control tools. `browser` is for Chromium automation. `files` and `run()` operate as the same unprivileged user who owns the desktop.

```python
from smolvm import SmolVM

with SmolVM.computer() as computer:
    print(computer.display.viewer_url)
    print(computer.browser.cdp_url)
    computer.files.write("/workspace/task.txt", "Review this file")
    result = computer.run("ls -la /workspace")
    print(result.stdout)
```

CDP is Chrome DevTools Protocol, the local address automation libraries use to control Chromium. It can be absent if a person closes Chromium without closing the computer. Start it again with:

```python
computer.browser.launch()
```

## Use it from TypeScript

```ts
import { SmolVM } from "@celestoai/smolvm";

const smolvm = new SmolVM();
const computer = await smolvm.computers.create();

try {
  console.log(computer.display.viewerUrl);
  console.log(computer.browser.cdpUrl);
  await computer.files.write("/workspace/task.txt", "Review this file");
  const result = await computer.exec("ls -la /workspace");
  console.log(result.stdout);
} finally {
  await smolvm.close();
}
```

If Chromium was closed, open it again without replacing the computer:

```ts
await computer.browser.launch();
console.log(computer.browser.cdpUrl);
```

## What is included

The `linux-desktop` template includes Chromium, LXTerminal, PCManFM, Mousepad, Tint2, Openbox, Git, Wget, SSH tools, jq, Zip, and Unzip. It is intentionally small rather than a full GNOME or KDE installation.

Viewer, VNC, and CDP addresses listen only on your machine. Keep them in trusted application code. Do not send them to browser JavaScript or remote users.
