import os
import sys
from PySide6 import QtWidgets, QtGui, QtCore

def regenerate():
    # Set offscreen platform
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    app = QtWidgets.QApplication(sys.argv)
    
    from GUI.icon import build_app_icon
    icon = build_app_icon()
    
    # Paths
    icon_dir = "GUI/assets/icons"
    png_dir = os.path.join(icon_dir, "png")
    iconset_dir = os.path.join(icon_dir, "upbrpy_zircon.iconset")
    
    for d in [png_dir, iconset_dir]:
        if not os.path.exists(d):
            os.makedirs(d)

    sizes = [16, 32, 64, 128, 256, 512, 1024]
    
    # Save standard PNGs
    for size in sizes:
        pixmap = icon.pixmap(QtCore.QSize(size, size))
        pixmap.save(os.path.join(png_dir, f"upbrpy_zircon_{size}.png"))
    
    # Save iconset PNGs (macOS naming convention)
    iconset_sizes = [
        (16, "16x16"), (32, "16x16@2x"),
        (32, "32x32"), (64, "32x32@2x"),
        (128, "128x128"), (256, "128x128@2x"),
        (256, "256x256"), (512, "256x256@2x"),
        (512, "512x512"), (1024, "512x512@2x")
    ]
    
    for size, name in iconset_sizes:
        pixmap = icon.pixmap(QtCore.QSize(size, size))
        pixmap.save(os.path.join(iconset_dir, f"icon_{name}.png"))

    # Try to save ICO using Pillow
    ico_path = os.path.join(icon_dir, "upbrpy_zircon.ico")
    try:
        from PIL import Image
        print("Pillow available. Creating multi-size ICO...")
        img_paths = [os.path.join(png_dir, f"upbrpy_zircon_{s}.png") for s in [16, 32, 48, 64, 128, 256] if s <= 256]
        # Ensure we have a 48px one even if not in standard list
        p48 = os.path.join(png_dir, "upbrpy_zircon_48.png")
        if not os.path.exists(p48):
             icon.pixmap(QtCore.QSize(48, 48)).save(p48)
             if p48 not in img_paths:
                 img_paths.append(p48)
        
        # Open all and save as ICO
        images = [Image.open(p) for p in img_paths if os.path.exists(p)]
        # Sort by size to be safe
        images.sort(key=lambda x: x.width)
        images[0].save(ico_path, format='ICO', sizes=[(img.width, img.height) for img in images])
        print(f"ICO saved to {ico_path} using Pillow.")
    except ImportError:
        print("Pillow NOT available. Falling back to Qt 256px ICO...")
        pixmap = icon.pixmap(QtCore.QSize(256, 256))
        pixmap.save(ico_path, "ICO")
        print(f"ICO saved to {ico_path} using Qt.")

if __name__ == "__main__":
    regenerate()
