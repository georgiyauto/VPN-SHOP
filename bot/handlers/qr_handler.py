"""QR-код для ссылки подписки — пользователь сканирует вместо копирования URL."""
import io
import logging
from aiogram import Router, F
from aiogram.types import CallbackQuery, BufferedInputFile

from db.database import AsyncSessionLocal
from db.models import User
from utils.i18n import t

router = Router()
logger = logging.getLogger(__name__)


@router.callback_query(F.data == "show_qr")
async def show_qr(callback: CallbackQuery):
    import os
    try:
        import qrcode
        from qrcode.image.pure import PyPNGImage
    except ImportError:
        await callback.answer("qrcode не установлен", show_alert=True)
        return

    async with AsyncSessionLocal() as session:
        user = await session.get(User, callback.from_user.id)
        if not user:
            await callback.answer("Пользователь не найден", show_alert=True)
            return
        lang = (user.language or "ru")
        sub_token = str(user.sub_token)

    bot_domain = os.getenv("BOT_DOMAIN", "")
    sub_url = f"{bot_domain}/sub/{sub_token}"

    # Генерируем QR
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=4,
    )
    qr.add_data(sub_url)
    qr.make(fit=True)

    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)

    await callback.message.answer_photo(
        photo=BufferedInputFile(buf.read(), filename="subscription_qr.png"),
        caption=(
            f"📷 <b>QR-код подписки</b>\n\n"
            f"Отсканируйте камерой телефона или через приложение Hiddify/v2rayNG.\n\n"
            f"<code>{sub_url}</code>"
        ),
        parse_mode="HTML"
    )
    await callback.answer()
