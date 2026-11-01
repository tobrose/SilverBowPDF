"""Help > SilverbowPDF Guide: what the app does and how, with its shortcuts.

Plain HTML for a QTextBrowser, kept in code so it ships inside the app and
works with no network. It describes the app as it is -- when a feature or a
shortcut changes, change this too (tests/test_help.py checks every shortcut
it names against the real menu actions).
"""

from __future__ import annotations

GUIDE_HTML = """
<style>
  h1 { font-size: 18pt; margin-bottom: 2px; }
  h2 { font-size: 13pt; margin-top: 18px; margin-bottom: 4px; }
  td { padding: 2px 10px 2px 0; vertical-align: top; }
  .k { font-family: Consolas, monospace; white-space: nowrap; }
  .note { color: #707078; }
</style>

<h1>SilverbowPDF Guide</h1>
<p>View, mark up, fill in, sign and rearrange PDFs. Everything you change can be
undone (<span class="k">Ctrl+Z</span>) until you save, and what you see on screen
is exactly what gets saved.</p>

<h2>Opening and tabs</h2>
<ul>
<li><b>Open</b> a PDF with <span class="k">Ctrl+O</span>, by dragging it onto the
window, or by double-clicking it in Explorer or an email. Each document opens in
its own tab; opening one that's already open just switches to its tab.</li>
<li>Switch tabs with <span class="k">Ctrl+Tab</span> / <span class="k">Ctrl+Shift+Tab</span>
(or <span class="k">Ctrl+PgDn</span> / <span class="k">Ctrl+PgUp</span>). A dot on a tab
means it has unsaved changes.</li>
<li><b>Close</b> a tab with its &times;, File &gt; Close, <span class="k">Ctrl+W</span> or
<span class="k">Ctrl+F4</span>. You're asked about unsaved changes first.</li>
<li>Password-protected PDFs ask for their password when opened.</li>
</ul>

<h2>Moving around</h2>
<table>
<tr><td class="k">PgUp / PgDn</td><td>Previous / next page</td></tr>
<tr><td class="k">Ctrl+Home / Ctrl+End</td><td>First / last page</td></tr>
<tr><td class="k">Ctrl+G</td><td>Go to a page number (or type it in the page box on the toolbar)</td></tr>
<tr><td class="k">Ctrl++ / Ctrl+- / Ctrl+0</td><td>Zoom in / out / actual size</td></tr>
<tr><td class="k">Ctrl+1 / Ctrl+2</td><td>Fit width / fit the whole page</td></tr>
<tr><td class="k">Ctrl + mouse wheel</td><td>Zoom where the mouse is</td></tr>
<tr><td>Pan button, or drag with the middle mouse button</td><td>Drag the pages around; <span class="k">Esc</span> goes back to Select</td></tr>
<tr><td class="k">F4</td><td>Show or hide the page thumbnails</td></tr>
</table>

<h2>Pages</h2>
<p>Page commands act on the pages selected in the thumbnails sidebar while it's
showing (click, <span class="k">Ctrl</span>+click or <span class="k">Shift</span>+click), or
else on the current page. Right-click a thumbnail for the same commands on the selected
pages, or right-click a page for that page alone.</p>
<table>
<tr><td class="k">Ctrl+Shift+L / Ctrl+Shift+R</td><td>Rotate left / right</td></tr>
<tr><td>Drag thumbnails, or <span class="k">Alt+Up / Alt+Down</span></td><td>Move pages earlier / later</td></tr>
<tr><td class="k">Ctrl+Shift+D</td><td>Delete pages (or <span class="k">Delete</span> in the sidebar)</td></tr>
<tr><td class="k">Ctrl+Shift+I</td><td>Insert pages from other PDFs (or drop PDFs on the sidebar)</td></tr>
<tr><td class="k">Ctrl+Shift+E</td><td>Extract pages to a new PDF</td></tr>
</table>

<h2>Text</h2>
<ul>
<li>Drag across text to select it, or double-click a word. A small bar under the
selection offers <b>Highlight</b> (in the current colour, or pick one),
<b>Underline</b> and <b>Strikethrough</b>.</li>
<li><span class="k">Ctrl+C</span> copies the selection; <span class="k">Ctrl+A</span> selects
all the text on the current page (in the thumbnails, it selects every page). Right-click
selected text to copy or mark it.</li>
<li><b>Find</b> with <span class="k">Ctrl+F</span>: <span class="k">Enter</span> or
<span class="k">F3</span> for the next match, <span class="k">Shift+Enter</span> or
<span class="k">Shift+F3</span> for the previous one, <span class="k">Esc</span> to close.
Options match case and whole words.</li>
<li>Scanned pages have no text until you run <b>Tools &gt; Recognize Text (OCR)</b>,
which uses Windows' own text recognition (or right-click pages to recognize just those).
Save to keep the result.</li>
</ul>

<h2>Comments and markup</h2>
<p>The <b>Annotate</b> button on the toolbar holds every markup tool:</p>
<ul>
<li><b>Note</b>: click to place a sticky note.</li>
<li><b>Highlight, Underline, Strikethrough</b>: drag across text. They stay on until
you press <span class="k">Esc</span>.</li>
<li><b>Typewriter</b>: click and type. The bar above the text sets its colour and size.
Click away, press <span class="k">Esc</span> or <span class="k">Ctrl+Enter</span> to finish.
Double-click any text box to edit it; drag its edge to resize it.</li>
<li><b>Rectangle, Ellipse, Line, Arrow</b>: drag to draw (hold <span class="k">Shift</span>
for a square, a circle or 45&deg; angles). <b>Pen</b>: draw freehand until you press
<span class="k">Esc</span>. Shape Style sets their colour and line width.</li>
<li><b>Stamps</b>: pick one (Approved, Reviewed and Received carry your name and the
date), then click to place it.</li>
<li><b>Highlight Colour</b> and <b>Text Style</b> set the colours and sizes the tools use.</li>
</ul>
<p>With the <b>Select</b> tool, click any comment or mark to select it, drag it to move
it, and press <span class="k">Delete</span> to remove it. Highlights, underlines and
strikethroughs stay on their text: dragging across one selects that text. Point at a
comment to read it; double-click one to edit it. Drag the edge or corner of a text box, rectangle, ellipse, stamp or signature
picture to resize it &mdash; stamps and signature pictures keep their proportions.</p>

<h2>Forms</h2>
<ul>
<li>With the Select tool, click a field to fill it in. <span class="k">Tab</span> /
<span class="k">Shift+Tab</span> move between text fields; check boxes toggle when
clicked; radio buttons pick one of a group; dropdowns show their choices.</li>
<li>The <b>Form</b> button makes forms: <b>Edit Fields</b> to move, resize or delete fields
(double-click one for its name and options), or draw a new <b>Text Field</b>,
<b>Multi-line Text Field</b>, <b>Checkbox</b> or <b>Dropdown</b>.</li>
</ul>

<h2>Signatures</h2>
<ul>
<li>The <b>Sign</b> button on the toolbar (or Tools &gt; Sign) holds the signing commands.
<b>Sign with Certificate</b>: drag a box where the signature goes (or click for a
standard size), then pick a digital ID &mdash; a certificate from Windows (smart cards included), a
<span class="k">.pfx</span> file, or one you create with <b>Create a Digital ID</b>. You
can add your signature picture, a reason and a location. You choose where the signed
copy is saved, and it then opens.</li>
<li><b>Place My Signature Picture</b> puts your signature image on the page,
without a certificate. <b>Choose Signature Picture</b> picks the image (a photo or
scan of your signature works; the paper is made transparent).</li>
<li>When a signed PDF opens, a bar says whether its signatures are valid;
<b>Details&hellip;</b> (or <b>Signatures in This Document</b>) lists who signed and when. Saving changes to a signed PDF breaks
its signatures, and you're warned before that happens.</li>
</ul>

<h2>Saving, printing and protecting</h2>
<table>
<tr><td class="k">Ctrl+S / Ctrl+Shift+S</td><td>Save / save as</td></tr>
<tr><td class="k">Ctrl+Z / Ctrl+Y</td><td>Undo / redo (until you save)</td></tr>
<tr><td class="k">Ctrl+P</td><td>Print: a preview first, then Windows' print dialog. What prints is what's on screen, unsaved changes included.</td></tr>
<tr><td>File &gt; Password Protection</td><td>Add, change or remove a password needed to open the document (applied when you save). Protected documents stay protected when saved.</td></tr>
</table>
<p>Some PDFs restrict what can be done with them (printing, copying, changing).
A yellow bar says what's restricted; <b>Unlock&hellip;</b> takes the owner password.</p>

<h2>Single-letter shortcuts</h2>
<p>Off unless you turn them on in <b>Tools &gt; Single-Letter Tool Shortcuts</b>
(your administrator may have set this for you):</p>
<table>
<tr><td class="k">V / N</td><td>Select / Note</td></tr>
<tr><td class="k">H / U / S</td><td>Highlight / Underline / Strikethrough</td></tr>
<tr><td class="k">T</td><td>Typewriter</td></tr>
<tr><td class="k">R / E / L / A / P</td><td>Rectangle / Ellipse / Line / Arrow / Pen</td></tr>
</table>
<p><span class="k">Esc</span> always returns to the Select tool, whatever this setting.</p>

<h2>Help</h2>
<table>
<tr><td class="k">F1</td><td>This guide</td></tr>
<tr><td>Help &gt; Check for Updates</td><td>Is there a newer version?</td></tr>
<tr><td>Help &gt; About SilverbowPDF</td><td>The version you're running, and where its error log is</td></tr>
</table>
"""
