// htmx injects a <style> tag with default .htmx-indicator rules on load, which
// this app doesn't use (see body.htmx-request::after in main.scss) and which
// trips the style-src CSP directive since it isn't in the allow-listed hashes.
htmx.config.includeIndicatorStyles = false;
