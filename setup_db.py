"""
Запусти ОДИН раз перед первым стартом бота:
    python setup_db.py

Создаёт базу и добавляет первый бизнес. VELOR AI универсален —
подставь сюда данные любого бизнеса (кофейня, барбершоп, автосервис,
студия, доставка — что угодно).
"""
import database

database.init_db()  # создаём/дополняем таблицы

# Настройки первого бизнеса. Меняй под себя:
BUSINESS_NAME = "Мой бизнес"
BUSINESS_ABOUT = "малый бизнес: приём заказов и заявок"   # чем занимается — для ИИ
BUSINESS_GREETING = (
    "Здравствуйте! Напишите, что вам нужно — я приму заявку и всё оформлю."
)

if not database.get_business(1):
    bid = database.create_business(
        name=BUSINESS_NAME,
        about=BUSINESS_ABOUT,
        greeting=BUSINESS_GREETING,
    )
    print(f"Создан бизнес «{BUSINESS_NAME}» с id = {bid}")
else:
    print("Бизнес №1 уже существует — ничего не меняю.")

print("База готова: файл assistant.db")
