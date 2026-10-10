import getpass
import os
import psycopg2
import discord
from discord.ext import commands, tasks
from datetime import datetime, timedelta

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ==========================================
# ⚙️ إعدادات البوت والمهلة الزمنية
# ==========================================
STUDY_CHANNEL_ID = 1323411341033341020  # آي دي قناة الشات المخصصة للصور
VOICE_CHANNEL_ID = 1323411341192597558  # آي دي روم المذاكرة الصوتية
STUDY_TIMEOUT_MINUTES = 25              # مهلة إرسال الصورة (بالدقائق)
# ==========================================

# رابط قاعدة البيانات السحابية من متغيرات البيئة
DATABASE_URL = os.getenv("DATABASE_URL")

# القواميس المؤقتة في الذاكرة
user_last_proof = {}  # {user_id: datetime}
user_join_time = {}   # {user_id: datetime}
warned_users = set()  # لتتبع من تم تنبيهه لمنع الإزعاج

def get_db_connection():
    """اتصال بقاعدة البيانات السحابية PostgreSQL"""
    return psycopg2.connect(DATABASE_URL, sslmode='require')

def init_db():
    """إنشاء الجدول في قاعدة البيانات السحابية إذا لم يكن موجوداً"""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_stats (
            user_id BIGINT PRIMARY KEY,
            daily INTEGER DEFAULT 0,
            weekly INTEGER DEFAULT 0,
            monthly INTEGER DEFAULT 0,
            last_date TEXT,
            last_week INTEGER
        )
    ''')
    conn.commit()
    cursor.close()
    conn.close()

def get_current_date_week():
    now = datetime.now()
    return now.strftime("%Y-%m-%d"), now.isocalendar()[1]

def update_user_minutes(user_id, minutes):
    """تحديث الدقائق في السحابة مع معالجة التصفير اليومي والأسبوعي"""
    today_str, current_week = get_current_date_week()
    
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute("SELECT daily, weekly, monthly, last_date, last_week FROM user_stats WHERE user_id = %s", (user_id,))
    row = cursor.fetchone()
    
    if row:
        daily, weekly, monthly, last_date, last_week = row
        
        if last_date != today_str:
            daily = 0
            last_date = today_str
            
        if last_week != current_week:
            weekly = 0
            last_week = current_week
            
        daily += minutes
        weekly += minutes
        monthly += minutes
        
        cursor.execute('''
            UPDATE user_stats 
            SET daily = %s, weekly = %s, monthly = %s, last_date = %s, last_week = %s
            WHERE user_id = %s
        ''', (daily, weekly, monthly, last_date, last_week, user_id))
    else:
        cursor.execute('''
            INSERT INTO user_stats (user_id, daily, weekly, monthly, last_date, last_week)
            VALUES (%s, %s, %s, %s, %s, %s)
        ''', (user_id, minutes, minutes, minutes, today_str, current_week))
        
    conn.commit()
    cursor.close()
    conn.close()

@bot.event
async def on_ready():
    if DATABASE_URL:
        init_db()
        print(f'تم تسجيل الدخول بنجاح باسم: {bot.user.name} ومتصل بقاعدة البيانات السحابية بنجاح!')
    else:
        print("⚠️ تنبيه: لم يتم العثور على رابط قاعدة البيانات DATABASE_URL!")
    check_study_proofs.start()

# تتبع دخول وخروج الأعضاء من الروم الصوتية
@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot:
        return
    
    if after.channel and after.channel.id == VOICE_CHANNEL_ID and before.channel != after.channel:
        user_join_time[member.id] = datetime.now()

    elif before.channel and before.channel.id == VOICE_CHANNEL_ID and after.channel != before.channel:
        if member.id in user_join_time:
            time_spent = datetime.now() - user_join_time[member.id]
            minutes = int(time_spent.total_seconds() // 60)
            if minutes > 0:
                update_user_minutes(member.id, minutes)
            del user_join_time[member.id]
            user_last_proof.pop(member.id, None)
            warned_users.discard(member.id)

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    if message.channel.id == STUDY_CHANNEL_ID and message.attachments:
        if any(att.content_type and att.content_type.startswith('image/') for att in message.attachments):
            now = datetime.now()
            user_last_proof[message.author.id] = now
            warned_users.discard(message.author.id)
            
            next_deadline = now + timedelta(minutes=STUDY_TIMEOUT_MINUTES)
            formatted_time = next_deadline.strftime("%I:%M %p")
            
            await message.add_reaction("✅")
            await message.reply(f"تمام عاش يا بطل! تم تسجيل إثبات المذاكرة. معاك مهلة لحد الساعة **{formatted_time}** ⏳")

    await bot.process_commands(message)

# أمر الوقت الباقي
@bot.command(name="remaining", aliases=["باقي", "الوقت"])
async def check_remaining_time(ctx):
    user_id = ctx.author.id
    
    if user_id not in user_join_time and user_id not in user_last_proof:
        await ctx.send(f"❌ {ctx.author.mention} أنت لست داخل روم المذاكرة الصوتية أو لم تبدأ جلستك بعد!")
        return

    now = datetime.now()
    start_time = user_last_proof.get(user_id) or user_join_time.get(user_id)
    deadline = start_time + timedelta(minutes=STUDY_TIMEOUT_MINUTES)
    remaining_delta = deadline - now
    
    total_seconds = int(remaining_delta.total_seconds())
    
    if total_seconds <= 0:
        await ctx.send(f"⚠️ {ctx.author.mention} لقد انتهت مهلتك تماماً! قم بإرسال صورة الإثبات فوراً وإلا سيتم إخراجك.")
        return
        
    mins = total_seconds // 60
    secs = total_seconds % 60
    
    await ctx.send(f"⏳ {ctx.author.mention} باقي لك **{mins} دقيقة و {secs} ثانية** على انتهاء المهلة الحالية.")

# أمر إحصائيات المستخدم الفردي
@bot.command(name="stats", aliases=["احصائيات"])
async def show_stats(ctx, target_member: discord.Member = None):
    member = target_member or ctx.author
    
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT daily, weekly, monthly FROM user_stats WHERE user_id = %s", (member.id,))
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    
    daily, weekly, monthly = (row[0], row[1], row[2]) if row else (0, 0, 0)
    
    current_session = 0
    if member.id in user_join_time:
        current_session = int((datetime.now() - user_join_time[member.id]).total_seconds() // 60)

    daily_total = daily + current_session
    weekly_total = weekly + current_session
    monthly_total = monthly + current_session

    embed = discord.Embed(
        title=f"📊 إحصائيات مذاكرة: {member.display_name}",
        color=discord.Color.blue()
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    
    embed.add_field(name="📅 اليوم", value=f"**{daily_total}** دقيقة ({round(daily_total/60, 1)} س)", inline=True)
    embed.add_field(name="🗓️ هذا الأسبوع (تراكمي)", value=f"**{weekly_total}** دقيقة ({round(weekly_total/60, 1)} س)", inline=True)
    embed.add_field(name="📆 هذا الشهر", value=f"**{monthly_total}** دقيقة ({round(monthly_total/60, 1)} س)", inline=True)
    
    if current_session > 0:
        embed.set_footer(text=f"🔥 الجلسة الحالية مستمرة منذ {current_session} دقيقة")

    await ctx.send(embed=embed)

# أمر المتصدرين
@bot.command(name="top", aliases=["المتصدرين"])
async def leaderboard(ctx):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, weekly FROM user_stats")
    rows = cursor.fetchall()
    cursor.close()
    conn.close()

    totals = {row[0]: row[1] for row in rows}
    for u_id, join_t in user_join_time.items():
        active_mins = int((datetime.now() - join_t).total_seconds() // 60)
        totals[u_id] = totals.get(u_id, 0) + active_mins

    if not totals:
        await ctx.send("لا توجد بيانات مذاكرة مسجلة حتى الآن!")
        return

    sorted_users = sorted(totals.items(), key=lambda x: x[1], reverse=True)[:5]
    
    embed = discord.Embed(
        title="🏆 متصدري المذاكرة (المجموع التراكمي الأسبوعي)",
        color=discord.Color.gold()
    )
    
    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
    description = ""
    
    for i, (u_id, weekly_mins) in enumerate(sorted_users):
        user = bot.get_user(u_id) or await bot.fetch_user(u_id)
        name = user.display_name if user else "عضو"
        description += f"{medals[i]} **{name}**: {weekly_mins} دقيقة ({round(weekly_mins/60, 1)} ساعة)\n"
        
    embed.description = description
    await ctx.send(embed=embed)

# اللوب المسؤول عن التحذير والطرد
@tasks.loop(minutes=1)
async def check_study_proofs():
    voice_channel = bot.get_channel(VOICE_CHANNEL_ID)
    study_channel = bot.get_channel(STUDY_CHANNEL_ID)
    
    if not voice_channel:
        return

    now = datetime.now()
    for member in voice_channel.members:
        if member.bot:
            continue
        
        last_time = user_last_proof.get(member.id)
        
        if not last_time:
            join_t = user_join_time.get(member.id, now)
            elapsed = (now - join_t).total_seconds() / 60
        else:
            elapsed = (now - last_time).total_seconds() / 60

        warning_threshold = max(0, STUDY_TIMEOUT_MINUTES - 5)
        if warning_threshold <= elapsed < STUDY_TIMEOUT_MINUTES:
            if member.id not in warned_users:
                warned_users.add(member.id)
                remaining_mins = int(STUDY_TIMEOUT_MINUTES - elapsed)
                if study_channel:
                    await study_channel.send(f"⚠️ {member.mention} باقي لك حوالي **{remaining_mins} دقائق** وتتطرد من الروم! اسرع وابعث صورة المذاكرة 📸")

        if elapsed > STUDY_TIMEOUT_MINUTES:
            try:
                await member.move_to(None)
                warned_users.discard(member.id)
                if study_channel:
                    await study_channel.send(f"🚨 {member.mention} تم إخراجك من روم المذاكرة الصوتية لعدم إرسال إثبات المذاكرة خلال الـ {STUDY_TIMEOUT_MINUTES} دقيقة الماضية!")
            except Exception as e:
                print(f"حدث خطأ أثناء نقل العضو {member}: {e}")

token = os.getenv("DISCORD_BOT_TOKEN")
if not token:
    token = getpass.getpass("Enter Discord bot token: ")
if not token:
    raise RuntimeError("A Discord bot token is required to start the bot.")

bot.run(token)
