from fastapi import FastAPI
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response


app = FastAPI(title="stage5-web-fixture")


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/allowed")
async def allowed():
    return PlainTextResponse("Stage 5 fixture page.\nThis content is safe to preview.\n")


@app.get("/redirect-blocked")
async def redirect_blocked():
    return RedirectResponse("http://blocked.test/blocked", status_code=302)


@app.get("/blocked")
async def blocked():
    return PlainTextResponse("blocked host body\n")


@app.get("/binary")
async def binary():
    return Response(content=b"\x00\x01\x02\x03", media_type="application/octet-stream")


@app.get("/large")
async def large():
    return PlainTextResponse("x" * 20000)


@app.get("/browser/rendered")
async def browser_rendered():
    return HTMLResponse(
        """
<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <meta name="description" content="Stage 6 browser fixture description" />
    <title>Loading...</title>
    <style>
      body {
        background: #f6f2e8;
        color: #1f2933;
        font-family: Georgia, serif;
        margin: 0;
      }
      main {
        margin: 48px auto;
        max-width: 720px;
        padding: 32px;
        background: #fffdf8;
        border: 2px solid #d7c2a0;
      }
      .eyebrow {
        letter-spacing: 0.08em;
        text-transform: uppercase;
        font-size: 12px;
      }
      h1 {
        margin-top: 12px;
      }
    </style>
  </head>
  <body>
    <main>
      <div class="eyebrow">Stage 6 Fixture</div>
      <h1 id="headline">Booting browser fixture</h1>
      <p id="body">Waiting for trusted browser rendering...</p>
    </main>
    <script>
      setTimeout(() => {
        document.title = "Stage 6 Fixture Title";
        document.getElementById("headline").textContent = "Stage 6 fixture rendered body";
        document.getElementById("body").textContent =
          "This rendered text comes from a deterministic JS fixture.";
      }, 50);
    </script>
  </body>
</html>
""".strip()
    )


@app.get("/browser/blocked-subresource")
async def browser_blocked_subresource():
    return HTMLResponse(
        """
<!doctype html>
<html>
  <head>
    <title>Blocked subresource</title>
  </head>
  <body>
    <p>Attempting blocked subresource.</p>
    <img src="http://blocked.test/browser/blocked-image.png" alt="blocked" />
  </body>
</html>
""".strip()
    )


@app.get("/browser/popup")
async def browser_popup():
    return HTMLResponse(
        """
<!doctype html>
<html>
  <head>
    <title>Popup fixture</title>
  </head>
  <body>
    <p>Popup attempt fixture.</p>
    <script>
      setTimeout(() => {
        window.open("http://allowed.test/browser/popup-target", "_blank");
      }, 50);
    </script>
  </body>
</html>
""".strip()
    )


@app.get("/browser/popup-target")
async def browser_popup_target():
    return HTMLResponse("<html><body><p>popup target</p></body></html>")


@app.get("/browser/download-page")
async def browser_download_page():
    return HTMLResponse(
        """
<!doctype html>
<html>
  <head>
    <title>Download fixture</title>
  </head>
  <body>
    <p>Download attempt fixture.</p>
    <script>
      setTimeout(() => {
        window.location = "http://allowed.test/browser/download.bin";
      }, 50);
    </script>
  </body>
</html>
""".strip()
    )


@app.get("/browser/download.bin")
async def browser_download_bin():
    return Response(
        content=b"fixture-download",
        media_type="application/octet-stream",
        headers={"Content-Disposition": 'attachment; filename="fixture.bin"'},
    )


@app.get("/browser/redirect-blocked")
async def browser_redirect_blocked():
    return RedirectResponse("http://blocked.test/browser/rendered", status_code=302)


@app.get("/browser/blocked-image.png")
async def browser_blocked_image():
    return Response(content=b"\x89PNG\r\n\x1a\n", media_type="image/png")
