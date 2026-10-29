from PIL import Image

img = Image.open("doors.png")  # исходная картинка
w, h = img.size

left  = img.crop((0, 0, w // 2, h))
right = img.crop((w // 2, 0, w, h))

left.save("door_closed.png")
right.save("door_open.png")

left  = left.resize((80, 120), Image.LANCZOS)
right = right.resize((80, 120), Image.LANCZOS)


print("Готово: door_closed.png и door_open.png")
