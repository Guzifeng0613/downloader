const { spawn } = require("node:child_process");

async function delay(ms) {
  await new Promise((resolve) => setTimeout(resolve, ms));
}

async function fetchJson(url) {
  const response = await fetch(url);
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}`);
  }
  return response.json();
}

async function waitForEndpoint(port, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const version = await fetchJson(`http://127.0.0.1:${port}/json/version`);
      if (version.webSocketDebuggerUrl) {
        return true;
      }
    } catch (_) {
      // keep polling
    }
    await delay(250);
  }
  return false;
}

async function fetchTargets(port) {
  try {
    return await fetchJson(`http://127.0.0.1:${port}/json/list`);
  } catch (_) {
    return [];
  }
}

function isMediaUrl(url) {
  return /\.(m3u8|mpd|mp4|webm|flv|mov)(\?|$)/i.test(url || "");
}

async function main() {
  const [, , targetUrl, browserPath, timeoutArg, userDataDir] = process.argv;
  if (!targetUrl || !browserPath || !timeoutArg || !userDataDir) {
    process.exit(0);
  }

  const timeoutMs = Math.max(5000, Number(timeoutArg) * 1000 || 15000);
  const port = 9229 + Math.floor(Math.random() * 500);
  const browser = spawn(
    browserPath,
    [
      `--remote-debugging-port=${port}`,
      `--user-data-dir=${userDataDir}`,
      "--no-first-run",
      "--no-default-browser-check",
      "--disable-background-networking",
      "--headless=new",
      targetUrl,
    ],
    {
      stdio: "ignore",
      windowsHide: true,
    },
  );

  const cleanup = () => {
    if (!browser.killed) {
      browser.kill();
    }
  };

  process.on("exit", cleanup);
  process.on("SIGTERM", cleanup);
  process.on("SIGINT", cleanup);

  const mediaUrls = new Set();
  let socket;

  try {
    const ready = await waitForEndpoint(port, timeoutMs);
    if (!ready) {
      process.exit(0);
    }

    const targets = await fetchTargets(port);
    const pageTarget = targets.find((target) => target.type === "page") || {};
    if (!pageTarget.webSocketDebuggerUrl) {
      process.exit(0);
    }

    socket = new WebSocket(pageTarget.webSocketDebuggerUrl);
    let commandId = 0;
    const send = (method, params = {}) => {
      commandId += 1;
      socket.send(JSON.stringify({ id: commandId, method, params }));
      return commandId;
    };

    await new Promise((resolve) => {
      const timer = setTimeout(() => resolve(), timeoutMs);

      socket.addEventListener("open", () => {
        send("Network.enable");
        send("Page.enable");
        send("Page.navigate", { url: targetUrl });
      });

      socket.addEventListener("message", (event) => {
        try {
          const payload = JSON.parse(event.data.toString());
          if (payload.method === "Network.requestWillBeSent") {
            const requestUrl = payload.params?.request?.url || "";
            if (isMediaUrl(requestUrl)) {
              mediaUrls.add(requestUrl);
            }
          }
          if (payload.method === "Network.responseReceived") {
            const responseUrl = payload.params?.response?.url || "";
            if (isMediaUrl(responseUrl)) {
              mediaUrls.add(responseUrl);
            }
          }
          if (payload.method === "Page.loadEventFired") {
            setTimeout(() => resolve(), 1500);
          }
        } catch (_) {
          // ignore malformed messages
        }
      });

      socket.addEventListener("error", () => resolve());
      socket.addEventListener("close", () => clearTimeout(timer));
    });

    process.stdout.write(Array.from(mediaUrls).join("\n"));
  } finally {
    try {
      if (socket && socket.readyState === WebSocket.OPEN) {
        socket.close();
      }
    } catch (_) {
      // ignore close errors
    }
    cleanup();
  }
}

main();
