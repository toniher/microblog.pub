from fastapi import Request

from app import templates
from app.database import AsyncSession


async def redirect(
    request: Request,
    db_session: AsyncSession,
    url: str,
    template: str = "redirect.html",
) -> templates.TemplateResponse:
    """
    Similar to RedirectResponse, but uses a 200 response with HTML.

    Needed for remote redirects on form submission endpoints,
    since our CSP policy disallows remote form submission.
    https://github.com/w3c/webappsec-csp/issues/8#issuecomment-810108984

    `template` only selects the wording shown while the redirect happens
    (`redirect_to_remote_instance.html` says "your instance").
    """
    return await templates.render_template(
        db_session,
        request,
        template,
        {
            "request": request,
            "url": url,
        },
        headers={"Refresh": "0;url=" + url},
    )
