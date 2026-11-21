import discord
from discord.ext import commands
from discord import app_commands
import json
import os
import asyncio
import time
import aiohttp
import random
import string
from keep_alive import keep_alive
from pymongo import MongoClient
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

# ================= CẤU HÌNH THƯƠNG HIỆU & BOT =================
TOKEN = os.getenv("DISCORD_TOKEN")

if not TOKEN:
    raise RuntimeError(
        "Thiếu biến môi trường DISCORD_TOKEN! "
        "Hãy tạo file .env (khi chạy local) hoặc đặt DISCORD_TOKEN trong Environment Variables."
    )

MONGO_URI = os.getenv("MONGO_URI")
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME", "nexus_core")

if not MONGO_URI:
    raise RuntimeError(
        "Thiếu biến môi trường MONGO_URI! "
        "Hãy thêm MONGO_URI vào file .env (khi chạy local) hoặc Environment Variables trên Render."
    )

mongo_client = MongoClient(MONGO_URI)
mongo_db = mongo_client[MONGO_DB_NAME]
state_col = mongo_db["state"]
stocks_col = mongo_db["stocks"]

BOT_NAME = "EM KHÔNG THÍCH ANH!!"
CREATOR_NAME = "Tên Của Bạn"  # Tên người tạo hiển thị trên toàn bộ hệ thống
ADMIN_ROLE_NAME = "Admin"
LOG_CHANNEL_ID = 0  # Điền ID kênh Discord nhận thông báo hết hàng / giao dịch (0 = gửi tin nhắn riêng cho Admin)

BANNER_URL = "https://i.imgur.com/8nLFCVP.png"
THUMBNAIL_URL = "https://i.imgur.com/AfFp7pu.png"

SCRIPT_LOADSTRING = 'loadstring(game:HttpGet("https://raw.githubusercontent.com/your-repo/main/script.lua"))()'
KEY_LINK = "https://linkvertise.com/your-get-key-link"
DB_FILE = "database.json"
STOCK_DIR = "stock"

# Bảng màu chủ đạo (Hex)
COLOR_MAIN = 0x2B2D31      # Xám tối tràn viền Discord
COLOR_ACCENT = 0x00F0FF    # Xanh Neon Cyan
COLOR_SUCCESS = 0x00FF7F   # Xanh lá ngọc
COLOR_GOLD = 0xFFD700      # Vàng VIP
COLOR_DANGER = 0xFF3366    # Đỏ hồng

# Danh sách mặt hàng trong Shop (mỗi key tương ứng với file stock/<key>.txt)
SHOP_ITEMS = {
    "key_1day": {
        "emoji": "🔑",
        "name": "Key VIP 24 Giờ",
        "price": 10000,
        "desc": "Mở khóa toàn bộ tính năng Premium trong 1 ngày"
    },
    "key_vinhvien": {
        "emoji": "👑",
        "name": "Key VIP Vĩnh Viễn",
        "price": 100000,
        "desc": "Sử dụng trọn đời • Bỏ qua hệ thống vượt link"
    },
    "acc_max": {
        "emoji": "⚔️",
        "name": "Acc Blox Fruits Max Level",
        "price": 50000,
        "desc": "Trắng thông tin • Sẵn Tộc V4 & Song Kiếm Oden"
    }
}

# Khóa bất đồng bộ chống 2 người mua cùng lúc lấy trùng 1 dòng trong file .txt
stock_lock = asyncio.Lock()

# ================= XỬ LÝ DATABASE & KHO FILE .TXT =================
STATE_DOC_ID = "main_state"

def _default_state() -> dict:
    return {"users": {}, "giveaways": {}}

def ensure_stock_files():
    os.makedirs(STOCK_DIR, exist_ok=True)
    for item_key in SHOP_ITEMS:
        doc = stocks_col.find_one({"_id": item_key})
        if not doc:
            stocks_col.insert_one({"_id": item_key, "items": []})

def get_stock_count(item_key: str) -> int:
    doc = stocks_col.find_one({"_id": item_key}, {"items": 1})
    if not doc:
        return 0
    return len(doc.get("items", []))

def add_stock_items(item_key: str, raw_text: str) -> int:
    new_lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    if not new_lines:
        return 0
    stocks_col.update_one(
        {"_id": item_key},
        {"$push": {"items": {"$each": new_lines}}},
        upsert=True
    )
    return len(new_lines)

async def pop_stock_item(item_key: str):
    """Lấy phần tử đầu tiên từ kho trên MongoDB, xóa phần tử đó và trả về (item_code, remaining_count)"""
    async with stock_lock:
        def _pop_sync():
            doc = stocks_col.find_one({"_id": item_key})
            if not doc or not doc.get("items"):
                return None, 0
            items = list(doc["items"])
            purchased_item = items[0]
            remaining_items = items[1:]
            stocks_col.update_one(
                {"_id": item_key},
                {"$set": {"items": remaining_items}}
            )
            return purchased_item, len(remaining_items)

        return await asyncio.to_thread(_pop_sync)

def load_db():
    doc = state_col.find_one({"_id": STATE_DOC_ID})
    if not doc:
        data = _default_state()
        state_col.insert_one({"_id": STATE_DOC_ID, **data})
        return data
    return {
        "users": doc.get("users", {}),
        "giveaways": doc.get("giveaways", {})
    }

def save_db(data):
    payload = {
        "users": data.get("users", {}),
        "giveaways": data.get("giveaways", {})
    }
    state_col.update_one(
        {"_id": STATE_DOC_ID},
        {"$set": payload},
        upsert=True
    )

def get_user(user_id):
    db = load_db()
    uid = str(user_id)
    if uid not in db["users"]:
        db["users"][uid] = {"balance": 0, "total_deposited": 0, "inventory": []}
        save_db(db)
    elif "total_deposited" not in db["users"][uid]:
        db["users"][uid]["total_deposited"] = db["users"][uid]["balance"]
        save_db(db)
    return db["users"][uid]

# ================= KHỞI TẠO BOT =================
class NexusBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(
            command_prefix="!",
            intents=intents,
            status=discord.Status.online,
            activity=discord.Activity(type=discord.ActivityType.watching, name="Hệ Thống Script 24/7 ⚡")
        )
        self.start_time = time.time()

    async def setup_hook(self):
        ensure_stock_files()
        self.add_view(ScriptPanelView())
        self.add_view(TicketPanelView())
        self.add_view(CloseTicketView())
        self.add_view(GiveawayView())
        asyncio.create_task(resume_giveaways(self))

        # Đăng ký lại các nút Reroll cũ từ database.json để không bị liệt sau khi restart bot
        db = load_db()
        for gw in db.get("giveaways", {}).values():
            if gw.get("ended") and gw.get("gw_id") and gw.get("last_winners"):
                self.add_view(RerollButtonView(gw["gw_id"]))

        await self.tree.sync()
        print(f"[{BOT_NAME}] Đã khởi động, đồng bộ lệnh và kiểm tra kho file .txt!")

    async def on_ready(self):
        activity = discord.Activity(type=discord.ActivityType.watching, name="Hệ Thống Script 24/7 ⚡")
        await self.change_presence(status=discord.Status.online, activity=activity)

bot = NexusBot()

def apply_pro_footer(embed: discord.Embed):
    embed.set_footer(
        text=f"{BOT_NAME} • Developed by {CREATOR_NAME}",
        icon_url=bot.user.display_avatar.url if bot.user else None
    )
    embed.timestamp = datetime.now()
    return embed

async def notify_out_of_stock(guild: discord.Guild, item_key: str, buyer: discord.Member):
    """Gửi cảnh báo hết hàng tới kênh Log hoặc DM cho các thành viên có role Admin"""
    item = SHOP_ITEMS[item_key]
    alert_embed = discord.Embed(
        title="🚨 CẢNH BÁO HẾT HÀNG TRONG KHO",
        description=(
            f"Mặt hàng **{item['emoji']} {item['name']}** vừa được bán hết sạch!\n\n"
            f"• **File kho:** `stock/{item_key}.txt`\n"
            f"• **Số lượng còn lại:** `0`\n"
            f"• **Khách mua cuối:** {buyer.mention} (`{buyer.id}`)\n\n"
            f"👉 Dùng lệnh `/nhap_kho` để bổ sung thêm hàng mới."
        ),
        color=COLOR_DANGER
    )
    apply_pro_footer(alert_embed)

    if LOG_CHANNEL_ID and LOG_CHANNEL_ID != 0:
        channel = guild.get_channel(LOG_CHANNEL_ID)
        if channel:
            await channel.send(embed=alert_embed)
            return

    admin_role = discord.utils.get(guild.roles, name=ADMIN_ROLE_NAME)
    if admin_role:
        for member in admin_role.members:
            if not member.bot:
                try:
                    await member.send(embed=alert_embed)
                except discord.Forbidden:
                    pass

# ================= 1. HỆ THỐNG ROBLOX SCRIPT & GET KEY =================
class ScriptPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Lấy Script", emoji="📜", style=discord.ButtonStyle.success, custom_id="btn_get_script")
    async def get_script(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(
            title="⚡ MÃ NGUỒN SCRIPT CHÍNH THỨC",
            description=(
                "Sao chép toàn bộ đoạn mã bên dưới và dán vào Executor của bạn:\n"
                f"```lua\n{SCRIPT_LOADSTRING}\n```\n"
                "> 💡 **Lưu ý:** Hỗ trợ tốt nhất trên *Delta, Fluxus, Codex, Arceus X*."
            ),
            color=COLOR_SUCCESS
        )
        apply_pro_footer(embed)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="Get Key (24h)", emoji="🔑", style=discord.ButtonStyle.primary, custom_id="btn_get_key")
    async def get_key(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(
            title="🔐 CỔNG XÁC MINH & NHẬN KEY",
            description=(
                "Hệ thống yêu cầu vượt qua 1 liên kết xác minh để nhận Key miễn phí:\n\n"
                f"🔗 **Link nhận Key:** [Nhấn Vào Đây Để Tiếp Tục]({KEY_LINK})\n"
                "⏳ **Thời hạn sử dụng:** `24 Giờ` kể từ lúc kích hoạt."
            ),
            color=COLOR_ACCENT
        )
        apply_pro_footer(embed)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="Trạng Thái", emoji="📡", style=discord.ButtonStyle.secondary, custom_id="btn_status")
    async def check_status(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(title="📡 TRẠNG THÁI MÁY CHỦ & EXECUTOR", color=COLOR_MAIN)
        embed.add_field(name="🛡️ Anticheat Bypass", value="```diff\n+ HOẠT ĐỘNG (UNDETECTED)\n```", inline=False)
        embed.add_field(name="📱 Mobile Executors", value="🟢 Delta\n🟢 Codex\n🟢 Fluxus", inline=True)
        embed.add_field(name="💻 PC Executors", value="🟢 Wave\n🟢 Solara\n🟢 SynZ", inline=True)
        apply_pro_footer(embed)
        await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="setup_script", description="[Admin] Gửi bảng điều khiển lấy Script & Key")
@app_commands.checks.has_role(ADMIN_ROLE_NAME)
async def setup_script(interaction: discord.Interaction):
    embed = discord.Embed(
        title=f"💠 {BOT_NAME} — TRUNG TÂM SCRIPT TỰ ĐỘNG",
        description=(
            "Chào mừng bạn đến với hệ thống phân phối Script tự động.\n"
            "Sử dụng các nút tương tác phía dưới để thao tác nhanh:\n\n"
            "📜 **Lấy Script** — Nhận mã `loadstring` mới nhất\n"
            "🔑 **Get Key** — Vượt link nhận mã kích hoạt 24h\n"
            "📡 **Trạng Thái** — Kiểm tra tình trạng hoạt động & độ an toàn"
        ),
        color=COLOR_MAIN
    )
    embed.set_thumbnail(url=THUMBNAIL_URL)
    embed.set_image(url=BANNER_URL)
    apply_pro_footer(embed)

    await interaction.channel.send(embed=embed, view=ScriptPanelView())
    await interaction.response.send_message("✅ Đã triển khai bảng điều khiển Script!", ephemeral=True)

# ================= 2. HỆ THỐNG SHOP TỪ FILE .TXT & TOP =================
class ShopSelect(discord.ui.Select):
    def __init__(self):
        options = []
        for key, item in SHOP_ITEMS.items():
            stock_left = get_stock_count(key)
            stock_label = f"Còn {stock_left}" if stock_left > 0 else "HẾT HÀNG"
            options.append(
                discord.SelectOption(
                    label=f"{item['name']} • {item['price']:,}đ [{stock_label}]",
                    description=item['desc'],
                    emoji=item['emoji'],
                    value=key
                )
            )
        super().__init__(placeholder="🛒 Chọn gói dịch vụ bạn muốn thanh toán...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        item_key = self.values[0]
        item = SHOP_ITEMS[item_key]
        db = load_db()
        uid = str(interaction.user.id)
        user_data = get_user(uid)

        # Kiểm tra số dư trước
        if user_data["balance"] < item["price"]:
            embed_err = discord.Embed(
                title="❌ GIAO DỊCH THẤT BẠI",
                description=(
                    f"Tài khoản của bạn không đủ số dư để thực hiện giao dịch này.\n\n"
                    f"• **Giá sản phẩm:** `{item['price']:,} VNĐ`\n"
                    f"• **Số dư hiện tại:** `{user_data['balance']:,} VNĐ`\n"
                    f"• **Còn thiếu:** `{item['price'] - user_data['balance']:,} VNĐ`\n\n"
                    "👉 Vui lòng mở **Ticket** để nạp thêm số dư vào tài khoản."
                ),
                color=COLOR_DANGER
            )
            apply_pro_footer(embed_err)
            await interaction.response.send_message(embed=embed_err, ephemeral=True)
            return

        # Lấy và xóa 1 dòng thật từ file .txt
        delivered_code, remaining_stock = await pop_stock_item(item_key)
        if delivered_code is None:
            embed_out = discord.Embed(
                title="📦 MẶT HÀNG TẠM HẾT KHO",
                description=(
                    f"Rất tiếc, **{item['emoji']} {item['name']}** hiện đã hết hàng trong kho.\n"
                    "Tiền trong ví của bạn **chưa bị trừ**. Vui lòng quay lại sau hoặc mở **Ticket** để được hỗ trợ!"
                ),
                color=COLOR_DANGER
            )
            apply_pro_footer(embed_out)
            await interaction.response.send_message(embed=embed_out, ephemeral=True)
            await notify_out_of_stock(interaction.guild, item_key, interaction.user)
            return

        # Trừ tiền và lưu lịch sử giao dịch
        db["users"][uid]["balance"] -= item["price"]
        db["users"][uid]["inventory"].append(f"[{item['name']}] {delivered_code}")
        save_db(db)

        embed_ok = discord.Embed(
            title="🎉 THANH TOÁN THÀNH CÔNG",
            description=(
                f"Cảm ơn bạn đã tin tưởng sử dụng dịch vụ của **{BOT_NAME}**!\n\n"
                f"📦 **Gói đã mua:** {item['emoji']} **{item['name']}**\n"
                f"💳 **Đã thanh toán:** `{item['price']:,} VNĐ`\n"
                f"💰 **Số dư còn lại:** `{db['users'][uid]['balance']:,} VNĐ`\n\n"
                f"🔐 **Key / Tài khoản của bạn:**\n```text\n{delivered_code}\n```"
            ),
            color=COLOR_SUCCESS
        )
        apply_pro_footer(embed_ok)
        await interaction.response.send_message(embed=embed_ok, ephemeral=True)

        # Nếu vừa bán hết dòng cuối cùng thì cảnh báo Admin ngay
        if remaining_stock == 0:
            await notify_out_of_stock(interaction.guild, item_key, interaction.user)

class ShopView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)
        self.add_item(ShopSelect())

@bot.tree.command(name="shop", description="Mở cửa hàng tự động mua Key VIP và Acc Roblox")
async def shop(interaction: discord.Interaction):
    user_data = get_user(interaction.user.id)
    embed = discord.Embed(
        title=f"💎 CỬA HÀNG TỰ ĐỘNG — {BOT_NAME}",
        description=(
            f"👤 **Khách hàng:** {interaction.user.mention}\n"
            f"💰 **Số dư khả dụng:** `{user_data['balance']:,} VNĐ`\n"
            "──────────────────────────────"
        ),
        color=COLOR_GOLD
    )
    for key, item in SHOP_ITEMS.items():
        stock_left = get_stock_count(key)
        status_badge = f"🟢 Còn `{stock_left}`" if stock_left > 0 else "🔴 `Hết hàng`"
        embed.add_field(
            name=f"{item['emoji']} {item['name']} • `{item['price']:,} VNĐ` • {status_badge}",
            value=f"└ *{item['desc']}*",
            inline=False
        )
    embed.set_thumbnail(url=THUMBNAIL_URL)
    apply_pro_footer(embed)
    await interaction.response.send_message(embed=embed, view=ShopView(), ephemeral=True)

@bot.tree.command(name="nhap_kho", description="[Admin] Thêm nhanh danh sách Key/Acc vào file .txt")
@app_commands.describe(
    mat_hang="Chọn loại mặt hàng cần nhập kho",
    du_lieu="Dán danh sách Key hoặc Acc, mỗi mục xuống 1 dòng"
)
@app_commands.choices(mat_hang=[
    app_commands.Choice(name="Key VIP 24 Giờ (key_1day.txt)", value="key_1day"),
    app_commands.Choice(name="Key VIP Vĩnh Viễn (key_vinhvien.txt)", value="key_vinhvien"),
    app_commands.Choice(name="Acc Blox Fruits Max Level (acc_max.txt)", value="acc_max"),
])
@app_commands.checks.has_role(ADMIN_ROLE_NAME)
async def nhap_kho(interaction: discord.Interaction, mat_hang: str, du_lieu: str):
    added = add_stock_items(mat_hang, du_lieu)
    total = get_stock_count(mat_hang)
    item = SHOP_ITEMS[mat_hang]

    embed = discord.Embed(
        title="📥 CẬP NHẬT KHO HÀNG THÀNH CÔNG",
        description=(
            f"• **Mặt hàng:** {item['emoji']} **{item['name']}**\n"
            f"• **File lưu trữ:** `stock/{mat_hang}.txt`\n"
            f"• **Đã thêm:** `+{added}` mục mới\n"
            f"• **Tổng tồn kho hiện tại:** `{total}` mục"
        ),
        color=COLOR_SUCCESS
    )
    apply_pro_footer(embed)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="nap_tien", description="[Admin] Cộng số dư vào tài khoản thành viên")
@app_commands.checks.has_role(ADMIN_ROLE_NAME)
async def nap_tien(interaction: discord.Interaction, member: discord.Member, so_tien: int):
    db = load_db()
    get_user(member.id)
    uid = str(member.id)
    db["users"][uid]["balance"] += so_tien
    if so_tien > 0:
        db["users"][uid]["total_deposited"] += so_tien
    save_db(db)
    
    embed = discord.Embed(
        title="💳 BIÊN LAI NẠP TIỀN THÀNH CÔNG",
        description=(
            f"• **Người nhận:** {member.mention}\n"
            f"• **Số tiền cộng:** `+{so_tien:,} VNĐ`\n"
            f"• **Số dư hiện tại:** `{db['users'][uid]['balance']:,} VNĐ`\n"
            f"• **Tổng tích lũy:** `{db['users'][uid]['total_deposited']:,} VNĐ`"
        ),
        color=COLOR_SUCCESS
    )
    apply_pro_footer(embed)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="top", description="Xem bảng xếp hạng Top 10 Đại Gia nạp tiền nhiều nhất server")
async def top_leaderboard(interaction: discord.Interaction):
    db = load_db()
    users = db.get("users", {})
    
    sorted_users = sorted(
        [(uid, data) for uid, data in users.items() if data.get("total_deposited", 0) > 0],
        key=lambda x: x[1].get("total_deposited", 0),
        reverse=True
    )[:10]

    embed = discord.Embed(
        title=f"🏆 BẢNG XẾP HẠNG ĐẠI GIA — {BOT_NAME}",
        description="Vinh danh những nhà tài trợ & khách hàng VIP đóng góp nhiều nhất cho hệ thống:\n──────────────────────────────",
        color=COLOR_GOLD
    )
    embed.set_thumbnail(url=THUMBNAIL_URL)

    if not sorted_users:
        embed.add_field(name="📭 Chưa có dữ liệu", value="Hiện chưa có thành viên nào phát sinh giao dịch nạp tiền.", inline=False)
    else:
        medals = ["🥇", "🥈", "🥉"]
        rank_lines = []
        for index, (uid, data) in enumerate(sorted_users):
            rank_icon = medals[index] if index < 3 else f"`#{index + 1}`"
            total_dep = data.get("total_deposited", 0)
            rank_lines.append(f"{rank_icon} <@{uid}> — Tổng nạp: **`{total_dep:,} VNĐ`**")
        
        embed.add_field(name="👑 Top Thành Viên Đóng Góp", value="\n\n".join(rank_lines), inline=False)

    apply_pro_footer(embed)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="tui_do", description="Kiểm tra số dư và lịch sử giao dịch của bạn")
async def tui_do(interaction: discord.Interaction):
    user_data = get_user(interaction.user.id)
    inv_text = "\n".join(user_data["inventory"]) if user_data["inventory"] else "Chưa có giao dịch nào."
    
    embed = discord.Embed(
        title="🎒 KHO LƯU TRỮ CÁ NHÂN",
        description=f"Chủ sở hữu: {interaction.user.mention}",
        color=COLOR_ACCENT
    )
    embed.set_thumbnail(url=interaction.user.display_avatar.url)
    embed.add_field(name="💰 Số Dư Khả Dụng", value=f"```fix\n{user_data['balance']:,} VNĐ\n```", inline=True)
    embed.add_field(name="💎 Tổng Tích Lũy", value=f"```yaml\n{user_data['total_deposited']:,} VNĐ\n```", inline=True)
    embed.add_field(name="📦 Lịch Sử Key / Tài Khoản Đã Mua", value=f"```text\n{inv_text}\n```", inline=False)
    apply_pro_footer(embed)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ================= 3. TRA CỨU TÀI KHOẢN ROBLOX (API THẬT) =================
@bot.tree.command(name="roblox", description="Tra cứu thông tin chi tiết tài khoản Roblox từ API chính thức")
@app_commands.describe(username="Nhập chính xác tên đăng nhập (Username) Roblox")
async def roblox_lookup(interaction: discord.Interaction, username: str):
    await interaction.response.defer()

    async with aiohttp.ClientSession() as session:
        url_id = "https://users.roblox.com/v1/usernames/users"
        payload = {"usernames": [username], "excludeBannedUsers": False}
        async with session.post(url_id, json=payload) as resp:
            if resp.status != 200:
                await interaction.followup.send("❌ Máy chủ API Roblox đang bận, vui lòng thử lại sau!")
                return
            data = await resp.json()
            if not data.get("data"):
                err_embed = discord.Embed(
                    title="🔍 KHÔNG TÌM THẤY NGƯỜI CHƠI",
                    description=f"Không tồn tại tài khoản Roblox nào có tên **`{username}`**.",
                    color=COLOR_DANGER
                )
                apply_pro_footer(err_embed)
                await interaction.followup.send(embed=err_embed)
                return
            user_id = data["data"][0]["id"]

        profile_task = session.get(f"https://users.roblox.com/v1/users/{user_id}")
        avatar_task = session.get(f"https://thumbnails.roblox.com/v1/users/avatar-headshot?userIds={user_id}&size=420x420&format=Png&isCircular=false")
        friends_task = session.get(f"https://friends.roblox.com/v1/users/{user_id}/friends/count")
        followers_task = session.get(f"https://friends.roblox.com/v1/users/{user_id}/followers/count")
        presence_task = session.post("https://presence.roblox.com/v1/presence/users", json={"userIds": [user_id]})

        res_profile, res_avatar, res_friends, res_followers, res_presence = await asyncio.gather(
            profile_task, avatar_task, friends_task, followers_task, presence_task
        )

        profile = await res_profile.json()
        avatar_data = await res_avatar.json()
        friends_data = await res_friends.json()
        followers_data = await res_followers.json()
        presence_data = await res_presence.json()

    display_name = profile.get("displayName", username)
    real_name = profile.get("name", username)
    bio = profile.get("description") or "*Không có tiểu sử*"
    if len(bio) > 200:
        bio = bio[:197] + "..."
    
    is_banned = profile.get("isBanned", False)
    created_raw = profile.get("created", "")
    try:
        dt = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
        created_timestamp = int(dt.timestamp())
        created_str = f"<t:{created_timestamp}:D> (<t:{created_timestamp}:R>)"
    except Exception:
        created_str = "Không rõ"

    avatar_url = avatar_data["data"][0]["imageUrl"] if avatar_data.get("data") else THUMBNAIL_URL
    friends_count = friends_data.get("count", 0)
    followers_count = followers_data.get("count", 0)

    presence_type = 0
    last_location = "Ngoại tuyến"
    if presence_data.get("userPresences"):
        p_info = presence_data["userPresences"][0]
        presence_type = p_info.get("userPresenceType", 0)
        last_location = p_info.get("lastLocation", "Trực tuyến")

    status_map = {
        0: "⚫ Ngoại tuyến (Offline)",
        1: "🔵 Đang duyệt Web (Online)",
        2: f"🟢 Đang chơi game ({last_location})",
        3: "🟠 Đang mở Roblox Studio"
    }
    status_text = "🔴 Tài khoản đã bị khóa (Banned)" if is_banned else status_map.get(presence_type, "⚫ Ngoại tuyến")

    embed = discord.Embed(
        title=f"🎮 HỒ SƠ ROBLOX — {display_name}",
        url=f"https://www.roblox.com/users/{user_id}/profile",
        description=f"**Trạng thái:** {status_text}\n**Tiểu sử:** {bio}",
        color=COLOR_DANGER if is_banned else COLOR_ACCENT
    )
    embed.set_thumbnail(url=avatar_url)
    embed.add_field(name="🏷️ Tên Đăng Nhập", value=f"`@{real_name}`", inline=True)
    embed.add_field(name="🆔 User ID", value=f"`{user_id}`", inline=True)
    embed.add_field(name="📅 Ngày Tạo Tài Khoản", value=created_str, inline=False)
    embed.add_field(name="👥 Bạn Bè", value=f"`{friends_count:,}`", inline=True)
    embed.add_field(name="🌟 Người Theo Dõi", value=f"`{followers_count:,}`", inline=True)
    embed.add_field(name="🔗 Liên Kết Nhanh", value=f"[Mở Trang Cá Nhân Roblox](https://www.roblox.com/users/{user_id}/profile)", inline=True)

    apply_pro_footer(embed)
    await interaction.followup.send(embed=embed)

# ================= 4. HỆ THỐNG GIVEAWAY (ID RIÊNG + END BY ID + NÚT REROLL) =================
def generate_gw_id() -> str:
    chars = string.ascii_uppercase + string.digits
    return "GW-" + "".join(random.choices(chars, k=6))

def get_giveaway_by_id(gw_id: str):
    db = load_db()
    giveaways = db.get("giveaways", {})
    clean_id = gw_id.strip().upper()
    for msg_id, gw in giveaways.items():
        if gw.get("gw_id", "").upper() == clean_id:
            return msg_id, gw
    return None, None

def pick_winners_from_list(participants: list, winners_count: int) -> list:
    unique_users = list(dict.fromkeys(participants))
    if not unique_users:
        return []
    actual = min(max(1, winners_count), len(unique_users))
    return random.sample(unique_users, actual)

class RerollButtonView(discord.ui.View):
    def __init__(self, gw_id: str):
        super().__init__(timeout=None)
        self.gw_id = gw_id

        btn = discord.ui.Button(
            label="Reroll Người Thắng",
            emoji="🔄",
            style=discord.ButtonStyle.secondary,
            custom_id=f"gw_reroll_btn:{gw_id}"
        )
        btn.callback = self.reroll_callback
        self.add_item(btn)

    async def reroll_callback(self, interaction: discord.Interaction):
        has_admin_role = any(role.name == ADMIN_ROLE_NAME for role in interaction.user.roles)
        if not has_admin_role and not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                f"❌ Chỉ thành viên có vai trò **`{ADMIN_ROLE_NAME}`** mới được bấm **Reroll**!",
                ephemeral=True
            )
            return

        msg_id, gw = get_giveaway_by_id(self.gw_id)
        if not gw:
            await interaction.response.send_message("❌ Không tìm thấy dữ liệu Giveaway này.", ephemeral=True)
            return

        participants = gw.get("participants", [])
        if not participants:
            await interaction.response.send_message("❌ Giveaway này không có người tham gia để quay lại.", ephemeral=True)
            return

        winners = pick_winners_from_list(participants, gw.get("winners_count", 1))
        winner_mentions = ", ".join([f"<@{w}>" for w in winners])
        reroll_count = gw.get("reroll_count", 0) + 1

        gw["reroll_count"] = reroll_count
        gw["last_winners"] = winners
        db = load_db()
        db.setdefault("giveaways", {})[msg_id] = gw
        save_db(db)

        reroll_embed = discord.Embed(
            title=f"🔄 REROLL LẦN #{reroll_count} — {gw.get('gw_id')}",
            description=(
                f"🏆 **Phần thưởng:** `{gw.get('prize')}`\n"
                f"🥳 **Người chiến thắng mới:** {winner_mentions}\n"
                f"🔁 Thực hiện bởi: {interaction.user.mention}\n\n"
                "Vui lòng mở **Ticket** để nhận phần thưởng từ Admin!"
            ),
            color=COLOR_SUCCESS
        )
        apply_pro_footer(reroll_embed)
        await interaction.response.send_message(content=winner_mentions, embed=reroll_embed, view=RerollButtonView(self.gw_id))

class GiveawayView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Tham Gia Ngay", emoji="🎉", style=discord.ButtonStyle.primary, custom_id="gw_join_persistent")
    async def join_gw(self, interaction: discord.Interaction, button: discord.ui.Button):
        msg_id = str(interaction.message.id)
        db = load_db()
        gw = db.get("giveaways", {}).get(msg_id)

        if not gw or gw.get("ended"):
            await interaction.response.send_message("⚠️ Sự kiện Giveaway này đã kết thúc!", ephemeral=True)
            return

        if int(time.time()) >= gw["end_timestamp"]:
            await interaction.response.send_message("⏳ Giveaway đã hết giờ và đang chuẩn bị quay thưởng!", ephemeral=True)
            return

        uid = interaction.user.id
        participants = gw.get("participants", [])

        if uid in participants:
            participants.remove(uid)
            msg = "⚠️ Bạn đã hủy tham gia sự kiện Giveaway này."
        else:
            participants.append(uid)
            msg = "✅ Đăng ký tham gia thành công! Chúc bạn may mắn."

        gw["participants"] = participants
        db["giveaways"][msg_id] = gw
        save_db(db)

        button.label = f"Tham Gia Ngay ({len(participants)})"
        try:
            await interaction.message.edit(view=self)
        except discord.HTTPException:
            pass

        await interaction.response.send_message(msg, ephemeral=True)

async def announce_giveaway_result(channel: discord.abc.Messageable, msg_id: str, gw: dict):
    gw_id = gw.get("gw_id", "GW-UNKNOWN")
    prize = gw.get("prize", "Phần thưởng")
    participants = list(dict.fromkeys(gw.get("participants", [])))
    winners_count = int(gw.get("winners_count", 1))

    if not participants:
        empty_embed = discord.Embed(
            title=f"⏰ GIVEAWAY KẾT THÚC — {gw_id}",
            description=f"Sự kiện tặng **`{prize}`** đã kết thúc nhưng không có người tham gia.",
            color=COLOR_MAIN
        )
        apply_pro_footer(empty_embed)
        await channel.send(embed=empty_embed)
        return

    winners = pick_winners_from_list(participants, winners_count)
    winner_mentions = ", ".join([f"<@{w}>" for w in winners])

    gw["last_winners"] = winners
    db = load_db()
    db.setdefault("giveaways", {})[str(msg_id)] = gw
    save_db(db)

    win_embed = discord.Embed(
        title=f"🎊 CÔNG BỐ KẾT QUẢ GIVEAWAY — {gw_id} 🎊",
        description=(
            f"🏆 **Phần thưởng:** `{prize}`\n"
            f"🥳 **Người chiến thắng:** {winner_mentions}\n\n"
            "Vui lòng mở **Ticket** để nhận phần thưởng từ Admin!"
        ),
        color=COLOR_SUCCESS
    )
    apply_pro_footer(win_embed)

    await channel.send(
        content=winner_mentions,
        embed=win_embed,
        view=RerollButtonView(gw_id)
    )

async def finalize_giveaway(msg_id: str, force_early: bool = False):
    db = load_db()
    giveaways = db.get("giveaways", {})
    gw = giveaways.get(str(msg_id))
    if not gw or gw.get("ended"):
        return False

    gw["ended"] = True
    if force_early:
        gw["end_timestamp"] = int(time.time())
    giveaways[str(msg_id)] = gw
    save_db(db)

    channel = bot.get_channel(gw["channel_id"])
    if channel is None:
        try:
            channel = await bot.fetch_channel(gw["channel_id"])
        except discord.HTTPException:
            return False

    try:
        gw_msg = await channel.fetch_message(int(msg_id))
        disabled_view = GiveawayView()
        for child in disabled_view.children:
            child.disabled = True
            if hasattr(child, "label") and child.label and child.label.startswith("Tham Gia Ngay"):
                child.label = f"Đã Kết Thúc ({len(gw.get('participants', []))})"
        await gw_msg.edit(view=disabled_view)
    except discord.HTTPException:
        pass

    await announce_giveaway_result(channel, str(msg_id), gw)
    return True

async def finish_giveaway_task(msg_id: str, delay_seconds: float):
    if delay_seconds > 0:
        await asyncio.sleep(delay_seconds)
    await finalize_giveaway(str(msg_id), force_early=False)

async def resume_giveaways(client: commands.Bot):
    await client.wait_until_ready()
    db = load_db()
    giveaways = db.get("giveaways", {})
    now_ts = int(time.time())

    for msg_id_str, gw in list(giveaways.items()):
        if gw.get("ended"):
            continue
        remaining = gw["end_timestamp"] - now_ts
        asyncio.create_task(finish_giveaway_task(msg_id_str, max(0, remaining)))

@bot.tree.command(name="giveaway", description="[Admin] Tổ chức sự kiện Giveaway có ID riêng")
@app_commands.checks.has_role(ADMIN_ROLE_NAME)
async def giveaway(interaction: discord.Interaction, phan_thuong: str, phut: int, so_nguoi_thang: int = 1):
    if phut < 1:
        await interaction.response.send_message("❌ Thời gian Giveaway phải tối thiểu là `1` phút.", ephemeral=True)
        return
    if so_nguoi_thang < 1:
        await interaction.response.send_message("❌ Số người thắng phải tối thiểu là `1`.", ephemeral=True)
        return

    db = load_db()
    existing_ids = {item.get("gw_id", "").upper() for item in db.get("giveaways", {}).values()}
    gw_id = generate_gw_id()
    while gw_id in existing_ids:
        gw_id = generate_gw_id()

    end_time = datetime.now() + timedelta(minutes=phut)
    timestamp = int(end_time.timestamp())

    embed = discord.Embed(
        title="🎁 SỰ KIỆN GIVEAWAY ĐẶC BIỆT 🎁",
        description=(
            f"🆔 **Mã Giveaway:** `{gw_id}`\n"
            f"🏆 **Phần thưởng:** `{phan_thuong}`\n"
            f"👑 **Số giải thưởng:** `{so_nguoi_thang} người`\n"
            f"⏰ **Thời gian kết thúc:** <t:{timestamp}:R> (<t:{timestamp}:t>)\n"
            f"Host bởi: {interaction.user.mention}\n\n"
            "> *Nhấn vào nút **Tham Gia Ngay** bên dưới để ghi danh!*"
        ),
        color=COLOR_GOLD
    )
    embed.set_thumbnail(url=THUMBNAIL_URL)
    apply_pro_footer(embed)

    view = GiveawayView()
    await interaction.response.send_message(f"✅ Đã phát động Giveaway! Mã ID: **`{gw_id}`**", ephemeral=True)
    gw_msg = await interaction.channel.send(embed=embed, view=view)

    gw_data = {
        "gw_id": gw_id,
        "channel_id": interaction.channel.id,
        "end_timestamp": timestamp,
        "prize": phan_thuong,
        "winners_count": so_nguoi_thang,
        "host_id": interaction.user.id,
        "participants": [],
        "reroll_count": 0,
        "last_winners": [],
        "ended": False
    }
    db.setdefault("giveaways", {})[str(gw_msg.id)] = gw_data
    save_db(db)

    asyncio.create_task(finish_giveaway_task(str(gw_msg.id), phut * 60))

@bot.tree.command(name="end_giveaway", description="[Admin] Kết thúc sớm Giveaway theo ID riêng")
@app_commands.checks.has_role(ADMIN_ROLE_NAME)
async def end_giveaway(interaction: discord.Interaction, gw_id: str):
    msg_id, gw = get_giveaway_by_id(gw_id)
    if not gw:
        await interaction.response.send_message(
            f"❌ Không tìm thấy Giveaway nào có mã **`{gw_id.strip().upper()}`**.",
            ephemeral=True
        )
        return

    if gw.get("ended"):
        await interaction.response.send_message(
            f"ℹ️ Giveaway **`{gw['gw_id']}`** đã kết thúc trước đó rồi.",
            ephemeral=True
        )
        return

    await interaction.response.send_message(f"⏳ Đang kết thúc Giveaway **`{gw['gw_id']}`**...", ephemeral=True)
    await finalize_giveaway(msg_id, force_early=True)

# ================= 5. HỆ THỐNG TICKET HỖ TRỢ =================
class CloseTicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Đóng & Lưu Ticket", emoji="🔒", style=discord.ButtonStyle.danger, custom_id="close_ticket")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("🔒 Kênh hỗ trợ sẽ tự động đóng sau **3 giây**...")
        await asyncio.sleep(3)
        await interaction.channel.delete()

class TicketPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Mở Ticket Hỗ Trợ / Nạp Tiền", emoji="📩", style=discord.ButtonStyle.secondary, custom_id="open_ticket")
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            interaction.user: discord.PermissionOverwrite(read_messages=True, send_messages=True),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True)
        }
        channel = await guild.create_text_channel(name=f"ticket-{interaction.user.name}", overwrites=overwrites)
        
        embed = discord.Embed(
            title="🎫 PHÒNG HỖ TRỢ KHÁCH HÀNG",
            description=(
                f"Xin chào {interaction.user.mention},\n"
                "Đội ngũ quản trị viên đã nhận được yêu cầu của bạn.\n\n"
                "📌 **Vui lòng cung cấp thông tin:**\n"
                "• Ảnh chụp bill chuyển khoản / Mã thẻ cào (nếu nạp tiền)\n"
                "• Hình ảnh lỗi Executor (nếu cần hỗ trợ kỹ thuật)"
            ),
            color=COLOR_ACCENT
        )
        apply_pro_footer(embed)
        await channel.send(content=f"{interaction.user.mention}", embed=embed, view=CloseTicketView())
        await interaction.response.send_message(f"✅ Đã khởi tạo phòng hỗ trợ riêng tại {channel.mention}", ephemeral=True)

@bot.tree.command(name="setup_ticket", description="[Admin] Gửi bảng mở Ticket hỗ trợ khách hàng")
@app_commands.checks.has_role(ADMIN_ROLE_NAME)
async def setup_ticket(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🛠️ TRUNG TÂM CHĂM SÓC KHÁCH HÀNG",
        description=(
            "Bạn cần nạp tiền vào Shop, mua Key trực tiếp hoặc báo lỗi Script?\n"
            "Nhấn nút bên dưới để mở kênh làm việc riêng tư với đội ngũ Admin.\n\n"
            "⏱️ **Thời gian phản hồi trung bình:** `1 - 5 phút`"
        ),
        color=COLOR_MAIN
    )
    embed.set_thumbnail(url=THUMBNAIL_URL)
    apply_pro_footer(embed)
    await interaction.channel.send(embed=embed, view=TicketPanelView())
    await interaction.response.send_message("✅ Đã tạo bảng Ticket!", ephemeral=True)

# ================= 6. TRANG GIỚI THIỆU BOT & TÁC GIẢ (/ABOUT) =================
@bot.tree.command(name="about", description="Xem thông tin hệ thống và nhà phát triển Bot")
async def about(interaction: discord.Interaction):
    uptime_seconds = int(time.time() - bot.start_time)
    hours, remainder = divmod(uptime_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    embed = discord.Embed(
        title=f"💠 GIỚI THIỆU HỆ THỐNG — {BOT_NAME}",
        description=(
            f"**{BOT_NAME}** là hệ thống tự động hóa toàn diện được thiết kế chuyên biệt cho cộng đồng **Roblox Script & Gaming**.\n"
            "──────────────────────────────"
        ),
        color=COLOR_ACCENT
    )
    embed.set_thumbnail(url=bot.user.display_avatar.url)
    embed.add_field(name="👨‍💻 Nhà Phát Triển (Creator)", value=f"**{CREATOR_NAME}**", inline=True)
    embed.add_field(name="⚙️ Phiên Bản", value="`v3.1.0 Stock Pro`", inline=True)
    embed.add_field(name="📶 Độ Trễ (Ping)", value=f"`{round(bot.latency * 1000)}ms`", inline=True)
    
    embed.add_field(
        name="🚀 Tính Năng Cốt Lõi",
        value=(
            "• **Script & Key Loader:** Phân phối mã nguồn & vượt link tự động\n"
            "• **Auto Shop (.txt Stock):** Giao dịch hàng thật, tự xóa kho & cảnh báo hết hàng\n"
            "• **Roblox API Lookup & Top:** Tra cứu hồ sơ người chơi & Vinh danh Top đại gia\n"
            "• **Giveaway & Ticket:** Tổ chức sự kiện và chăm sóc khách hàng"
        ),
        inline=False
    )
    embed.add_field(name="⏱️ Thời Gian Hoạt Động", value=f"`{hours} giờ {minutes} phút {seconds} giây`", inline=False)
    
    apply_pro_footer(embed)
    await interaction.response.send_message(embed=embed)

# ================= 7. QUẢN LÝ SERVER =================
@bot.tree.command(name="clear", description="[Admin] Dọn dẹp tin nhắn trong kênh")
@app_commands.checks.has_role(ADMIN_ROLE_NAME)
async def clear(interaction: discord.Interaction, so_luong: int):
    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=so_luong)
    await interaction.followup.send(f"🧹 Đã dọn dẹp sạch sẽ `{len(deleted)}` tin nhắn.")

# ================= HỆ THỐNG VERIFY THÀNH VIÊN =================
VERIFIED_ROLE_NAME = "Verified"  # Tên Role sẽ cấp sau khi xác minh thành công
MIN_ACCOUNT_AGE_DAYS = 3       # Yêu cầu tài khoản Discord tạo tối thiểu bao nhiêu ngày (0 = không giới hạn)

class VerifyPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Xác Minh Ngay",
        emoji="✅",
        style=discord.ButtonStyle.success,
        custom_id="btn_verify_account"
    )
    async def verify_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        role = discord.utils.get(interaction.guild.roles, name=VERIFIED_ROLE_NAME)
        if not role:
            await interaction.response.send_message(
                f"❌ Không tìm thấy Role **`{VERIFIED_ROLE_NAME}`** trong server. Hãy báo Admin tạo Role này trước!",
                ephemeral=True
            )
            return

        if role in interaction.user.roles:
            await interaction.response.send_message(
                "ℹ️ Tài khoản của bạn đã được xác minh trước đó rồi.",
                ephemeral=True
            )
            return

        # Kiểm tra tuổi tài khoản Discord chống acc clone mới tạo
        if MIN_ACCOUNT_AGE_DAYS > 0:
            account_age = datetime.now(interaction.user.created_at.tzinfo) - interaction.user.created_at
            if account_age.days < MIN_ACCOUNT_AGE_DAYS:
                await interaction.response.send_message(
                    f"❌ Tài khoản của bạn mới tạo được `{account_age.days}` ngày. Server yêu cầu tài khoản tối thiểu `{MIN_ACCOUNT_AGE_DAYS}` ngày mới có thể xác minh!",
                    ephemeral=True
                )
                return

        try:
            await interaction.user.add_roles(role, reason="Xác minh thành viên qua nút bấm")
            embed = discord.Embed(
                title="🎉 XÁC MINH THÀNH CÔNG",
                description=(
                    f"Chào mừng {interaction.user.mention}!\n"
                    f"Bạn đã được cấp vai trò {role.mention} và có thể truy cập toàn bộ kênh của server."
                ),
                color=COLOR_SUCCESS
            )
            apply_pro_footer(embed)
            await interaction.response.send_message(embed=embed, ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ Bot thiếu quyền **Quản Lý Vai Trò (Manage Roles)** hoặc Role của Bot đang nằm dưới Role `Verified`.\n"
                "Hãy vào **Cài đặt máy chủ -> Vai trò** và kéo Role của Bot lên cao hơn Role `Verified`!",
                ephemeral=True
            )

@bot.tree.command(name="setup_verify", description="[Admin] Gửi bảng xác minh thành viên bằng nút bấm")
@app_commands.checks.has_role(ADMIN_ROLE_NAME)
async def setup_verify(interaction: discord.Interaction):
    embed = discord.Embed(
        title=f"🛡️ XÁC MINH TÀI KHOẢN — {BOT_NAME}",
        description=(
            "Để mở khóa toàn bộ kênh chat, Shop và khu vực Script của máy chủ, "
            "vui lòng nhấn vào nút **Xác Minh Ngay** bên dưới.\n\n"
            f"• **Vai trò nhận được:** `@ {VERIFIED_ROLE_NAME}`\n"
            f"• **Điều kiện tài khoản:** Tối thiểu `{MIN_ACCOUNT_AGE_DAYS}` ngày tuổi"
        ),
        color=COLOR_MAIN
    )
    embed.set_thumbnail(url=THUMBNAIL_URL)
    apply_pro_footer(embed)

    await interaction.channel.send(embed=embed, view=VerifyPanelView())
    await interaction.response.send_message("✅ Đã gửi bảng Verify!", ephemeral=True)

keep_alive()
bot.run(TOKEN)
