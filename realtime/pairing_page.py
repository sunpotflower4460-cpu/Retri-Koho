"""HTML responses for the signed pairing confirmation screen."""

from html import escape

from fastapi.responses import HTMLResponse


def approval_page(title, content="", *, status_code=200):
    return HTMLResponse(
        f"""<!doctype html>
<html lang="ja">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{escape(title)} — Re:trip</title>
    <link rel="stylesheet" href="/style.css?v=approval-link">
  </head>
  <body>
    <main class="pairing-review">
      <section class="pairing-review-card" aria-labelledby="approval-title">
        <div class="pairing-review-brand">Re:trip</div>
        <h1 id="approval-title">{escape(title)}</h1>
        {content}
      </section>
    </main>
  </body>
</html>""",
        status_code=status_code,
    )


def confirmation_page(approval_id, signature, *, code, created_at, expires_at):
    approval_id = escape(approval_id, quote=True)
    signature = escape(signature, quote=True)
    created_at = escape(created_at, quote=True)
    expires_at = escape(expires_at, quote=True)
    code = escape(code)
    return approval_page(
        "Muse のペアリング申請",
        f"""<dl class="pairing-review-details">
          <div><dt>申請者</dt><dd>Muse</dd></div>
          <div><dt>確認コード</dt><dd class="pairing-number">{code}</dd></div>
          <div><dt>作成</dt><dd><time datetime="{created_at}">{created_at.replace("T", " ").replace("Z", " UTC")}</time></dd></div>
          <div><dt>期限</dt><dd><time datetime="{expires_at}">{expires_at.replace("T", " ").replace("Z", " UTC")}</time></dd></div>
        </dl>
        <form class="pairing-review-actions" method="post" action="/pairing/approve/{approval_id}">
          <input type="hidden" name="t" value="{signature}">
          <button class="pairing-review-approve" type="submit">承認する</button>
          <button type="submit" formaction="/pairing/reject/{approval_id}">却下する</button>
        </form>""",
    )
