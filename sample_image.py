#!/usr/bin/env python
from PIL import Image, ImageDraw

# Create a simple image (a red square with some text)
img = Image.new('RGB', (200, 200), color = (255, 255, 255))
draw = ImageDraw.Draw(img)
draw.rectangle([(20, 20), (180, 180)], fill=(255, 0, 0))
draw.text((40, 100), "Nextcloud MCP", fill=(255, 255, 255))
img.save('sample_image.png')

print("Image created successfully: sample_image.png")
