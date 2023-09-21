from aiogram.utils.keyboard import InlineKeyboardBuilder, InlineKeyboardButton

keyb = InlineKeyboardBuilder()
keyb.add(InlineKeyboardButton(text='➕ Добавить заметку', callback_data='AddNote'),
         InlineKeyboardButton(text='📃 Список заметок', callback_data='ListNote'),
         InlineKeyboardButton(text='🔍 Найти заметку', callback_data='FindNote'),
         InlineKeyboardButton(text='⛔ Удалить все заметки', callback_data='RemoveAll'))
keyb.adjust(2)

go_menu = InlineKeyboardBuilder()
go_menu.add(InlineKeyboardButton(text='◀', callback_data='go_menu'))

