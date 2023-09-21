import asyncio
import logging
from aiogram import F
from aiogram import Bot, Dispatcher, types
from aiogram.filters.command import Command
from app.functions import *
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.storage.memory import MemoryStorage
from app.config import *
from app.keyboard import *

logging.basicConfig(level=logging.INFO)
bot = Bot(token=TOKEN_BOT, disable_web_page_preview=True, parse_mode='HTML')
dp = Dispatcher(storage=MemoryStorage())


class AddNote(StatesGroup):
    NoteName = State()
    NoteDesc = State()


class FindNote(StatesGroup):
    NoteText = State()


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer("Добро пожаловать в бота Заметок!", reply_markup=keyb.as_markup())


@dp.callback_query(F.data == 'AddNote')
async def AddNote_callback_query(callback: types.CallbackQuery, state: FSMContext):
    await bot.delete_message(chat_id=callback.from_user.id, message_id=callback.message.message_id)
    await bot.send_message(chat_id=callback.from_user.id, text='<b>Напишите название заметки</b>')
    await state.set_state(AddNote.NoteName)


@dp.callback_query(F.data == 'go_menu')
async def go_menu_callback(callback: types.CallbackQuery):
    await bot.edit_message_text(chat_id=callback.from_user.id, message_id=callback.message.message_id,
                                text='Добро пожаловать в бота Заметок!', reply_markup=keyb.as_markup())


@dp.callback_query(F.data == 'FindNote')
async def FindNote_callback_query(callback: types.CallbackQuery, state: FSMContext):
    await bot.send_message(chat_id=callback.from_user.id, text='Введите текст заметки')
    await state.set_state(FindNote.NoteText)


@dp.callback_query(F.data == 'ListNote')
async def ListNote_callback_query(callback: types.CallbackQuery):
    notes_list = await list_note()
    notes = InlineKeyboardBuilder()
    for lists in notes_list:
        note_id = lists[0]
        note_name = lists[1]
        notes.add(InlineKeyboardButton(text=note_name, callback_data=f'note_{note_id}'))
    notes.add(InlineKeyboardButton(text='◀', callback_data='go_menu'))
    notes.adjust(2)
    await bot.edit_message_text(chat_id=callback.from_user.id, message_id=callback.message.message_id,
                                text='Список заметок', reply_markup=notes.as_markup())

@dp.callback_query(F.data == 'RemoveAll')
async def remove_all_callback(callback:types.CallbackQuery):
    await delete_all()
    await bot.edit_message_text(chat_id=callback.from_user.id, message_id=callback.message.message_id, text='Все заметки успешно удалены!', reply_markup=go_menu.as_markup())


@dp.callback_query(F.data.startswith('removenote_'))
async def RemoteNote_callback(callback: types.CallbackQuery):
    note_id = callback.data.split('_')[-1]
    print(note_id)
    await delete_note(note_id)
    await ListNote_callback_query(callback)


@dp.callback_query(F.data.startswith('note_'))
async def note_startwith(callback: types.CallbackQuery):
    note_id = callback.data.split('_')[-1]
    notes = await get_note_by_id(note_id)
    NoteName = notes[0]
    NoteDesc = notes[1]
    delete_and_back = InlineKeyboardBuilder()
    delete_and_back.add(InlineKeyboardButton(text='Удалить заметку', callback_data=f'removenote_{note_id}'),
                        InlineKeyboardButton(text='◀', callback_data='ListNote'))
    delete_and_back.adjust(1)
    print(f'removenote_{note_id}')
    text = (f'Название заметки: <b>{NoteName}</b>\n\n'
            f'Текст заметки:<b> {NoteDesc}</b>')
    await bot.edit_message_text(chat_id=callback.from_user.id, message_id=callback.message.message_id, text=text,
                                reply_markup=delete_and_back.as_markup())


@dp.message(AddNote.NoteName)
async def add_note_state(message: types.Message, state: FSMContext):
    await state.update_data(NoteName=message.text)
    await message.answer('<b>Введите текст заметки</b>')
    await state.set_state(AddNote.NoteDesc)


@dp.message(FindNote.NoteText)
async def find_note_state(message: types.Message, state: FSMContext):
    note_text = message.text
    lis = await find_note_by_text(note_text)
    buttons = InlineKeyboardBuilder()
    print(lis)
    if lis is False:
        await message.answer('Заметка не была найдена!', reply_markup=go_menu.as_markup())
    else:
        for ids in lis:
            listt = await get_note_by_id(ids[0])
            note_name = listt[0]
            buttons.add(InlineKeyboardButton(text=note_name, callback_data=f'note_{ids[0]}'))
        buttons.add(InlineKeyboardButton(text='◀', callback_data='go_menu'))
        buttons.adjust(2)
        await message.answer('Список заметок по вашему запросу: ', reply_markup=buttons.as_markup())

@dp.message(AddNote.NoteDesc)
async def add_note_desc_state(message: types.Message, state: FSMContext):
    data = await state.get_data()
    NoteName = data["NoteName"]
    NoteDesc = message.text
    await add_note(NoteName, NoteDesc)
    await message.answer('Заметка успешно добавлена!', reply_markup=go_menu.as_markup())
    await state.clear()


async def main():
    await database.create_db()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
