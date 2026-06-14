# Telegram Monospace Column Alignment Guide

This guide documents the layout and column alignment logic implemented for the FunPay diagnostic report. It explains how to prevent mobile rendering bugs and align multiline lists containing emojis and links.

## 1. Emoji Monospace Column Alignment
In Telegram, putting different emojis on different lines (e.g. `🧟` and `👤`) can cause alignment issues because different emojis have different widths in standard fonts. Moving the emojis outside the `<code>` block, followed by a single space, and using a fixed width monospace code block ensures perfect column alignment.

### Correct Approach (Emojis Outside Code Tags, Space Separator)
```html
🧟 <code>+PVE   8248₽  │ </code>🟣 Дорого
🔗 <code>         </code><b><code>*ТЫК*</code></b>
```
*Result:* Emojis and a single space are rendered outside the monospace block, ensuring all starting offsets and column boundaries (price, separator line, and link) are perfectly aligned.

---

## 2. Spacing & Column Width Logic

### Column Configuration
- **Max price width (`max_len`):** `7` characters.
- **Price padding:** `.rjust(7)` or `.rjust(max_len)`.
- **Status/Verdict indent:** align immediately after the vertical pipe `│`.
- **Link padding (`spaces_len`):** Dynamic based on price digits:
  - `10` spaces (centered under price) if all displayed prices are 1-digit (<10₽ or None).
  - `9` spaces (aligned under ruble sign) if any displayed price is 2 or more digits (>=10₽).

### Code Template (Python)

```python
# Widths
max_len = 7

# Dynamic spaces_len logic:
# spaces_len = 10 if (pve_price < 10 and nopve_price < 10) else 9
spaces_str = " " * spaces_len

pve_line = f"🧟 <code>+PVE   {pve_display}  │ </code>{verdict}"
link_line = f"🔗 <code>{spaces_str}</code><a href=\"{url}\"><b><code>*ТЫК*</code></b></a>"

nopve_line = f"👤 <code>-PVE   {nopve_display}  │ </code>{verdict}"
```

---

## 3. Separator Lines & Length
- **Dashes count:** 31 characters.
- **Character used:** `─` (U+2500 Box Drawings Light Horizontal).
- **Format:** Always wrap the separator line inside `<code>` tags so it doesn't wrap/break on narrow screens.
```python
report_msg = "\n\n<code>───────────────────────────────</code>\n\n".join(chunk)
```

---

## 4. Telegram Message Chunking
Telegram has a limit of **100 HTML tags/entities** per message. Large reports with nested `<a>` and `<code>` tags will fail to send if kept in a single message.
- **Chunk size:** `8` items per message.
- Separate chunks with the 31-dash line.
- Place the monitoring source footer (`📋 Автомониторинг: Git 🤖`) at the end of the last chunk.
