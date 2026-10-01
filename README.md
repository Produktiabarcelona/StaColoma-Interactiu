# Anem a Fondo · Pantalla interactiva

Interactive presentation for a touch screen, built from the presentation `Fondo.ppt`.

## Opening it

Open `index.html` in Chrome or Edge. In kiosk mode (full screen, no browser bars):

```bash
msedge --kiosk "file:///C:/ruta/al/projecte/index.html" --edge-kiosk-type=fullscreen
```

To use it on another computer, copy `index.html` and the `assets/` folder.

## Navigation

- **Slides 1–2**: presentation loop while the audience takes their seats. Tap anywhere → menu.
- **Menu**: 7 tiles that lead to each section. ◀ goes back to the cover to close the presentation.
- **Sections**: 🏠 goes to the menu; ◀ ▶ go to the previous / next slide.
- Keyboard: ← → navigate · `H` menu · `Esc` cover · `F` full screen.

## Updating the content

When the presentation changes (for example, to add a photo), save `Fondo.ppt` and run:

```bash
pip install numpy pillow scipy
python tools/build_assets.py
```

The script reads the texts, photos and tiles from the PowerPoint (it needs LibreOffice to convert a `.ppt`; with a `.pptx` it doesn't). It regenerates `assets/manifest.js` and the images.

## Debugging

- `index.html?slide=5` opens a specific slide.
- `index.html?slow=4` plays the transitions in slow motion.
- `index.html?slide=5&overlay` lays the original slide on top to check alignment.
