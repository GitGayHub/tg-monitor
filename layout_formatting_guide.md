# Telegram Monospace Column Alignment Guide

This guide documents the layout and column alignment logic implemented for the FunPay diagnostic report. It explains how to prevent mobile rendering bugs and align multiline lists containing emojis and links.

## 1. The Variable-Width Emoji Monospace Bug
In Telegram, putting emojis inside `<code>` or `<pre>` tags causes alignment issues on mobile devices (iOS/Android). Monospace fonts treat emojis as variable-width or double-width characters, which breaks alignment for all trailing text on that line.

### ❌ Broken Approach (Emoji Inside Code Tags)
```html
<code>🧟 +PVE   8248₽  │ </code>🟣 Дорого
<code>🔗          </code>*ТЫК*
```
*Result:* Monospace width is distorted on mobile, causing the vertical pipes (`│`) and link tabs to shift.

###  Correct Approach (Emoji Outside Code Tags)
```html
🧟<code> +PVE   8248₽  │ </code>🟣 Дорого
🔗<code>           </code>*ТЫК*
```
*Result:* Because the variable-width emojis (`🧟`, `👤`, `🔗`) are placed **outside** the `<code>` tag, they are rendered identically by the system font, and the monospace block starts at the exact same offset.

---

## 2. Spacing & Column Width Logic

### Column Configuration
- **Max price width (`max_len`):** `7` characters.
- **Price padding:** `.rjust(7)` or `.rjust(max_len)`.
- **Status/Verdict indent:** align immediately after the vertical pipe `│`.
- **Link padding (`spaces_len`):** `max_len + 3` (e.g. `7 + 3 = 10` spaces).

### Code Template (Python)

```python
# Widths
max_len = 7
spaces_len = max_len + 3
spaces_str = " " * spaces_len

# Emojis outside <code>, a single space inside <code> starts the monospace block
pve_line = f"🧟<code> +PVE   {pve_display}  │ </code>{verdict}"
link_line = f"🔗<code> {spaces_str}</code><a href=\"{url}\"><b>*ТЫК*</b></a>"

nopve_line = f"👤<code> -PVE   {nopve_display}  │ </code>{verdict}"
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
