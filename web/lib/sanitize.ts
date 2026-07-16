// Minimal HTML sanitizer for untrusted body_html rendered via dangerouslySetInnerHTML.
// It strips the dangerous elements and attributes that enable stored XSS (script/style/
// iframe/etc. tags, inline on* event handlers, and javascript:/non-image data: URLs),
// while leaving normal formatting (<p>, <strong>, <ul>, <a href>, <img src>…) intact.
//
// This is a pragmatic defense for a single-user internal tool, not a full sandbox — for
// belt-and-suspenders robustness, swap in a maintained sanitizer (e.g. isomorphic-dompurify).
const DANGEROUS_TAGS = "script|style|iframe|object|embed|link|meta|base|form|noscript|template";

export function sanitizeHtml(html: string | null | undefined): string {
  if (!html) return "";
  let out = String(html);
  // Remove dangerous elements together with their content.
  out = out.replace(new RegExp(`<\\s*(${DANGEROUS_TAGS})\\b[^>]*>[\\s\\S]*?<\\s*/\\s*\\1\\s*>`, "gi"), "");
  // Remove any remaining opening/self-closing dangerous tags.
  out = out.replace(new RegExp(`<\\s*/?\\s*(${DANGEROUS_TAGS})\\b[^>]*>`, "gi"), "");
  // Strip inline event handlers: onclick="…" / onerror='…' / onload=value.
  out = out.replace(/\son\w+\s*=\s*"[^"]*"/gi, "");
  out = out.replace(/\son\w+\s*=\s*'[^']*'/gi, "");
  out = out.replace(/\son\w+\s*=\s*[^\s>]+/gi, "");
  // Neutralize javascript:/non-image data: URLs in href/src.
  out = out.replace(/(href|src|xlink:href)\s*=\s*"(?:\s*javascript:|\s*data:(?!image\/))[^"]*"/gi, '$1="#"');
  out = out.replace(/(href|src|xlink:href)\s*=\s*'(?:\s*javascript:|\s*data:(?!image\/))[^']*'/gi, "$1='#'");
  return out;
}
