# Put the project logo here

Drop the logo in as **`logo.svg`** (preferred), or `logo.png` / `logo.jpg`.
Nothing else to change: the sidebar mark and the browser-tab icon both pick it
up, and the "CN" monogram is used only while no file is present.

* SVG is preferred because the mark is drawn at 32x32 in the sidebar and as a
  favicon; a small raster looks soft on high-DPI screens.
* PNG should be square and at least 128x128, with a transparent background -
  the sidebar has a light and a dark theme behind it.
* Files in this folder are served as-is at the site root: `public/logo.svg`
  is fetched as `/logo.svg`.
