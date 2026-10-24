# Синтетическое тестовое фото

`seedling.png` — растение в терракотовом горшке на деревянном столе.
Создано 25 октября 2026 встроенным инструментом OpenAI image_gen для тестов
эмулятора MAX. Исходные фотографии и сторонние изображения не использовались.
Людей, надписей и логотипов нет. Это синтетическая фикстура, а не размеченный
пример для оценки CV или подтверждение соответствия теме кампании.

Промпт: natural photograph of a small green seedling in a plain unbranded
terracotta pot on a light wooden table, soft daylight from a window, simple
neutral background, realistic leaf and soil texture, entire pot and plant
visible, square composition. No people, text, logos or watermark.

Каждое входящее тестовое фото пока ссылается на этот файл. Он отдаётся локальным
эмулятором по `/media/seedling.png` без авторизации. Разные `photo_id` и `token`
обозначают отдельные сообщения с одинаковым содержимым файла.
