import getpass
import os
import psycopg2
import discord
from discord.ext import commands, tasks
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ==========================================
# ⚙️ إعدادات البوت والمهلة الزمنية
# ==========================================
STUDY_CHANNEL_ID = 1323411341033341020
VOICE_CHANNEL_ID = 1323411341192597558
STUDY_TIMEOUT_MINUTES = 25
SUMMARY_USERNAME = "ilovemyself03821"
EGYPT_TZ = ZoneInfo("Africa/Cairo")  # توقيت مصر المحلي
# ==========================================

DATABASE_URL = os.getenv("DATABASE_URL")

user_last_proof = {}
user_join_time = {}
warned_users = set()
last_announced_round_status = None

def get_db_connection():
    return psycopg2.connect(DATABASE_URL, sslmode='require')

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_stats (
            user_id BIGINT PRIMARY KEY,
            daily INTEGER DEFAULT 0,
            weekly INTEGER DEFAULT 0,
            monthly INTEGER DEFAULT 0,
            last_date TEXT,
            last_week INTEGER,
            remaining_proof_minutes INTEGER DEFAULT 25,
            current_join_time TIMESTAMP WITH TIME ZONE
        )
    ''')
    cursor.execute('''
        ALTER TABLE user_stats 
        ADD COLUMN IF NOT EXISTS remaining_proof_minutes INTEGER DEFAULT 25,
        ADD COLUMN IF NOT EXISTS current_join_time TIMESTAMP WITH TIME ZONE;
    ''')
    conn.commit()
    cursor.close()
    conn.close()

async def create_default_roles(guild):
    role_names = {1: "Top 1", 2: "Top 2", 3: "Top 3", 4: "Top 4", 5: "Top 5"}
    da7e7_role_name = "الدحيح الشهر"

    try:
        for pos, rname in role_names.items():
            if not discord.utils.get(guild.roles, name=rname):
                await guild.create_role(name=rname, reason="Study Bot Leaderboard Role")
        
        if not discord.utils.get(guild.roles, name=da7e7_role_name):
            await guild.create_role(name=da7e7_role_name, color=discord.Color.gold(), reason="Monthly Top Role")
    except Exception as e:
        print(f"خطأ أثناء إنشاء الرتب تلقائياً في {guild.name}: {e}")

def get_current_date_week():
    now = datetime.now(EGYPT_TZ)
    return now.strftime("%Y-%m-%d"), now.isocalendar()[1]

def update_user_minutes(user_id, minutes):
    if minutes <= 0:
        return
    today_str, current_week = get_current_date_week()
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute("SELECT daily, weekly, monthly, last_date, last_week, remaining_proof_minutes FROM user_stats WHERE user_id = %s", (user_id,))
    row = cursor.fetchone()
    
    if row:
        daily, weekly, monthly, last_date, last_week, remaining_proof = row
        if last_date != today_str:
            daily = 0
            last_date = today_str
            remaining_proof = STUDY_TIMEOUT_MINUTES
        if last_week != current_week:
            weekly = 0
            last_week = current_week
            
        daily += minutes
        weekly += minutes
        monthly += minutes
        
        cursor.execute('''
            UPDATE user_stats 
            SET daily = %s, weekly = %s, monthly = %s, last_date = %s, last_week = %s, remaining_proof_minutes = %s
            WHERE user_id = %s
        ''', (daily, weekly, monthly, last_date, last_week, remaining_proof, user_id))
    else:
        cursor.execute('''
            INSERT INTO user_stats (user_id, daily, weekly, monthly, last_date, last_week, remaining_proof_minutes)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        ''', (user_id, minutes, minutes, minutes, today_str, current_week, STUDY_TIMEOUT_MINUTES))
        
    conn.commit()
    cursor.close()
    conn.close()

def get_or_reset_user_remaining_minutes(user_id):
    today_str, _ = get_current_date_week()
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT remaining_proof_minutes, last_date FROM user_stats WHERE user_id = %s", (user_id,))
    row = cursor.fetchone()
    
    if row:
        remaining_mins, last_date = row
        if last_date != today_str:
            remaining_mins = STUDY_TIMEOUT_MINUTES
            cursor.execute("UPDATE user_stats SET remaining_proof_minutes = %s, last_date = %s WHERE user_id = %s", (STUDY_TIMEOUT_MINUTES, today_str, user_id))
            conn.commit()
    else:
        remaining_mins = STUDY_TIMEOUT_MINUTES
        
    cursor.close()
    conn.close()
    return remaining_mins

def set_user_remaining_minutes(user_id, minutes):
    today_str, current_week = get_current_date_week()
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO user_stats (user_id, remaining_proof_minutes, last_date, last_week)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (user_id) DO UPDATE 
        SET remaining_proof_minutes = EXCLUDED.remaining_proof_minutes;
    ''', (user_id, minutes, today_str, current_week))
    conn.commit()
    cursor.close()
    conn.close()

def set_user_join_timestamp(user_id, timestamp):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE user_stats SET current_join_time = %s WHERE user_id = %s", (timestamp, user_id))
    conn.commit()
    cursor.close()
    conn.close()

def get_user_join_timestamp(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT current_join_time FROM user_stats WHERE user_id = %s", (user_id,))
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    return row[0] if row else None

@bot.event
async def on_ready():
    if DATABASE_URL:
        init_db()
        print(f'تم تسجيل الدخول بنجاح باسم: {bot.user.name} ومتصل بقاعدة البيانات السحابية بتوقيت مصر!')
    else:
        print("⚠️ تنبيه: لم يتم العثور على رابط قاعدة البيانات DATABASE_URL!")
    
    for guild in bot.guilds:
        await create_default_roles(guild)

    check_study_proofs.start()
    round_announcer.start()
    daily_summary_task.start()
    update_roles_task.start()

@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot:
        return
    
    study_channel = bot.get_channel(STUDY_CHANNEL_ID)
    now = datetime.now(EGYPT_TZ)

    # 1️⃣ عند الدخول لروم الصوت
    if after.channel and after.channel.id == VOICE_CHANNEL_ID and before.channel != after.channel:
        user_join_time[member.id] = now
        set_user_join_timestamp(member.id, now)
        
        remaining_mins = get_or_reset_user_remaining_minutes(member.id)
        
        if study_channel:
            await study_channel.send(
                f"👋 أهلاً بك يا {member.mention}! تم تسجيل دخولك لروم المذاكرة. "
                f"معك مهلة **{remaining_mins} دقيقة** لإرسال أول إثبات 📸⏳"
            )

    # 2️⃣ عند الخروج من روم الصوت (مع التحقق من عدم السبام لو أقل من دقيقة)
    elif before.channel and before.channel.id == VOICE_CHANNEL_ID and after.channel != before.channel:
        db_join_time = get_user_join_timestamp(member.id)
        start_time = user_join_time.get(member.id) or db_join_time or now
        
        time_spent = now - start_time
        session_minutes = int(time_spent.total_seconds() // 60)
        
        user_join_time.pop(member.id, None)
        set_user_join_timestamp(member.id, None)

        # لو العضو دخل وخرج بسرعة (أقل من دقيقة)، نتجاهل الرسالة تماماً لمنع السبام
        if session_minutes <= 0:
            return

        update_user_minutes(member.id, session_minutes)

        last_time = user_last_proof.get(member.id) or start_time
        elapsed = int((now - last_time).total_seconds() // 60)
        
        current_remaining = get_or_reset_user_remaining_minutes(member.id)
        new_remaining = max(0, current_remaining - elapsed)
        set_user_remaining_minutes(member.id, new_remaining)

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT daily, weekly FROM user_stats WHERE user_id = %s", (member.id,))
        row = cursor.fetchone()
        cursor.close()
        conn.close()

        daily_total = row[0] if row else session_minutes
        weekly_total = row[1] if row else session_minutes

        if study_channel:
            hours_daily = round(daily_total / 60, 1)
            hours_weekly = round(weekly_total / 60, 1)
            
            msg = (
                f"👋 **الله ينور يا {member.mention}!** خرجت من روم المذاكرة.\n"
                f"⏱️ **إنجازك في الجلسة الأخيرة:** `{session_minutes}` دقيقة.\n"
                f"📊 **رصيدك التراكمي المحفوظ:**\n"
                f"• مجموع اليوم: **{daily_total}** دقيقة ({hours_daily} ساعة)\n"
                f"• مجموع الأسبوع: **{weekly_total}** دقيقة ({hours_weekly} ساعة)\n"
                f"⏳ **المهلة المتبقية للإثبات:** `{new_remaining}` دقيقة.\n"
                f"✨ رصيدك محفوظ ومضمون 100%! 💪"
            )
            await study_channel.send(msg)

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    if message.channel.id == STUDY_CHANNEL_ID and message.attachments:
        if any(att.content_type and att.content_type.startswith('image/') for att in message.attachments):
            now = datetime.now(EGYPT_TZ)
            user_last_proof[message.author.id] = now
            warned_users.discard(message.author.id)
            
            set_user_remaining_minutes(message.author.id, STUDY_TIMEOUT_MINUTES)
            
            next_deadline = now + timedelta(minutes=STUDY_TIMEOUT_MINUTES)
            formatted_time = next_deadline.strftime("%I:%M %p")
            
            await message.add_reaction("✅")
            await message.reply(f"تمام عاش يا بطل! تم تسجيل إثبات المذاكرة وتجديد مهلتك لـ **25 دقيقة** حتى الساعة **{formatted_time}** ⏳")

    content = message.content.strip().lower()
    ctx = await bot.get_context(message)
    
    if not message.content.startswith(bot.command_prefix):
        if content in ["باقي", "الوقت", "الباقي", "remaining"]:
            ctx.command = bot.get_command("remaining")
            if ctx.command:
                await bot.invoke(ctx)
                return
        elif content in ["توب", "المتصدرين", "الترتيب", "top"]:
            ctx.command = bot.get_command("top")
            if ctx.command:
                await bot.invoke(ctx)
                return
        elif content in ["احصائيات", "إحصائيات", "ساعاتي", "معلوماتي", "ساعات", "ساعه", "وقتي", "مذاكرتي", "ستاتس", "stat", "stats"]:
            ctx.command = bot.get_command("stats")
            if ctx.command:
                await bot.invoke(ctx)
                return

    await bot.process_commands(message)

@bot.command(name="remaining", aliases=["باقي", "الوقت", "الباقي"])
async def check_remaining_time(ctx):
    user_id = ctx.author.id
    if user_id not in user_join_time and user_id not in user_last_proof:
        await ctx.send(f"❌ {ctx.author.mention} أنت لست داخل روم المذاكرة الصوتية أو لم تبدأ جلستك بعد!")
        return

    now = datetime.now(EGYPT_TZ)
    start_time = user_last_proof.get(user_id) or user_join_time.get(user_id) or now
    db_remaining = get_or_reset_user_remaining_minutes(user_id)
    
    deadline = start_time + timedelta(minutes=db_remaining)
    remaining_delta = deadline - now
    total_seconds = int(remaining_delta.total_seconds())
    
    if total_seconds <= 0:
        await ctx.send(f"⚠️ {ctx.author.mention} لقد انتهت مهلتك تماماً! أرسل صورة الإثبات فوراً وإلا سيتم إخراجك.")
        return
        
    mins = total_seconds // 60
    secs = total_seconds % 60
    await ctx.send(f"⏳ {ctx.author.mention} باقي لك **{mins} دقيقة و {secs} ثانية** على انتهاء المهلة الحالية.")

@bot.command(name="stats", aliases=["احصائيات", "إحصائيات", "ساعاتي", "معلوماتي", "ساعات", "ساعه", "وقتي", "مذاكرتي", "ستاتس", "stat"])
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
        current_session = int((datetime.now(EGYPT_TZ) - user_join_time[member.id]).total_seconds() // 60)

    daily_total = daily + current_session
    weekly_total = weekly + current_session
    monthly_total = monthly + current_session

    embed = discord.Embed(title=f"📊 إحصائيات مذاكرة: {member.display_name}", color=discord.Color.blue())
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="📅 اليوم", value=f"**{daily_total}** دقيقة ({round(daily_total/60, 1)} س)", inline=True)
    embed.add_field(name="🗓️ هذا الأسبوع (تراكمي)", value=f"**{weekly_total}** دقيقة ({round(weekly_total/60, 1)} س)", inline=True)
    embed.add_field(name="📆 هذا الشهر", value=f"**{monthly_total}** دقيقة ({round(monthly_total/60, 1)} س)", inline=True)
    
    if current_session > 0:
        embed.set_footer(text=f"🔥 الجلسة الحالية مستمرة منذ {current_session} دقيقة")

    await ctx.send(embed=embed)

@bot.command(name="top", aliases=["المتصدرين", "توب", "الترتيب"])
async def leaderboard(ctx):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT user_id, weekly FROM user_stats")
        rows = cursor.fetchall()
        cursor.close()
        conn.close()

        totals = {row[0]: row[1] for row in rows}
        for u_id, join_t in user_join_time.items():
            active_mins = int((datetime.now(EGYPT_TZ) - join_t).total_seconds() // 60)
            totals[u_id] = totals.get(u_id, 0) + active_mins

        if not totals:
            await ctx.send("لا توجد بيانات مذاكرة مسجلة حتى الآن في السيرفر!")
            return

        sorted_users = sorted(totals.items(), key=lambda x: x[1], reverse=True)[:5]
        
        embed = discord.Embed(title="🏆 متصدري المذاكرة (المجموع التراكمي الأسبوعي)", color=discord.Color.gold())
        medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
        description = ""
        
        for i, (u_id, weekly_mins) in enumerate(sorted_users):
            user_mention = f"<@{u_id}>"
            description += f"{medals[i]} {user_mention}: **{weekly_mins}** دقيقة ({round(weekly_mins/60, 1)} ساعة)\n"
            
        embed.description = description
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(f"حدث خطأ أثناء جلب قائمة المتصدرين: {e}")
        print(f"Error in leaderboard command: {e}")

@bot.command(name="setup_roles", aliases=["تجهيز_الرتب", "انشاء_الرتب"])
@commands.has_permissions(administrator=True)
async def setup_roles_command(ctx):
    await create_default_roles(ctx.guild)
    await ctx.send("✅ تم إنشاء وتجهيز جميع رتب المتصدرين بنجاح! تأكد الآن من رفع رتبة البوت فوقهم في إعدادات السيرفر.")

@tasks.loop(minutes=1)
async def round_announcer():
    global last_announced_round_status
    study_channel = bot.get_channel(STUDY_CHANNEL_ID)
    if not study_channel:
        return
    
    now = datetime.now(EGYPT_TZ)
    today_6am = now.replace(hour=6, minute=0, second=0, microsecond=0)
    if now < today_6am:
        today_6am -= timedelta(days=1)
    
    diff_mins = int((now - today_6am).total_seconds() // 60)
    if diff_mins < 0:
        return
    
    cycle_mins = 150
    position_in_cycle = diff_mins % cycle_mins
    round_num = (diff_mins // cycle_mins) + 1
    
    if position_in_cycle < 120:
        status = f"study_{round_num}"
    else:
        status = f"break_{round_num}"
    
    if last_announced_round_status is None:
        last_announced_round_status = status
        return
    
    if last_announced_round_status != status:
        last_announced_round_status = status
        if position_in_cycle < 120:
            await study_channel.send(f"🔔 **بدء الراوند رقم {round_num}!**\n⏰ وقت المذاكرة الجادة بدأ (ساعتان). شدوا حيلكم يا أبطال! 💪📚")
        else:
            await study_channel.send(f"☕ **وقت البريك للراوند {round_num}!**\nخذوا راحة لمدة 30 دقيقة، ارتاحوا واشربوا مية ورجعوا أقوى! 🧘‍♂️✨")

@tasks.loop(hours=24)
async def daily_summary_task():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, daily, weekly FROM user_stats ORDER BY daily DESC LIMIT 5")
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    
    if not rows:
        return

    summary_text = "📊 **الملخص اليومي لإحصائيات المذاكرة في السيرفر:**\n\n"
    for r in rows:
        u_id, daily, weekly = r
        summary_text += f"- User ID `{u_id}`: اليوم = {daily} دقيقة | الأسبوع = {weekly} دقيقة\n"
    
    for guild in bot.guilds:
        member = discord.utils.get(guild.members, name=SUMMARY_USERNAME) or discord.utils.get(guild.members, global_name=SUMMARY_USERNAME)
        if member:
            try:
                await member.send(summary_text)
                break
            except Exception as e:
                print(f"تعذر إرسال رسالة خاصة لـ {SUMMARY_USERNAME}: {e}")

@tasks.loop(minutes=30)
async def update_roles_task():
    for guild in bot.guilds:
        try:
            await create_default_roles(guild)
            
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT user_id FROM user_stats ORDER BY weekly DESC LIMIT 5")
            top_weekly = cursor.fetchall()
            
            cursor.execute("SELECT user_id FROM user_stats ORDER BY monthly DESC LIMIT 1")
            top_monthly = cursor.fetchone()
            cursor.close()
            conn.close()

            role_names = {1: "Top 1", 2: "Top 2", 3: "Top 3", 4: "Top 4", 5: "Top 5"}
            da7e7_role_name = "الدحيح الشهر"

            role_objs = {pos: discord.utils.get(guild.roles, name=rname) for pos, rname in role_names.items()}
            da7e7_role = discord.utils.get(guild.roles, name=da7e7_role_name)

            for pos, r_obj in role_objs.items():
                if r_obj:
                    for m in r_obj.members:
                        await m.remove_roles(r_obj)

            for index, (u_id,) in enumerate(top_weekly):
                pos = index + 1
                try:
                    member = guild.get_member(u_id) or await guild.fetch_member(u_id)
                    if member and pos in role_objs and role_objs[pos]:
                        await member.add_roles(role_objs[pos])
                except Exception as fetch_err:
                    print(f"تعذر جلب العضو {u_id} لإعطائه الرتبة: {fetch_err}")

            if top_monthly and da7e7_role:
                m_uid = top_monthly[0]
                for m in da7e7_role.members:
                    await m.remove_roles(da7e7_role)
                try:
                    monthly_member = guild.get_member(m_uid) or await guild.fetch_member(m_uid)
                    if monthly_member:
                        await monthly_member.add_roles(da7e7_role)
                except Exception as fetch_err:
                    print(f"تعذر جلب العضو {m_uid} لإعطائه رتبة الدحيح: {fetch_err}")

        except Exception as e:
            print(f"خطأ أثناء تحديث الرتب في السيرفر {guild.name}: {e}")

@tasks.loop(minutes=1)
async def check_study_proofs():
    voice_channel = bot.get_channel(VOICE_CHANNEL_ID)
    study_channel = bot.get_channel(STUDY_CHANNEL_ID)
    if not voice_channel:
        return

    now = datetime.now(EGYPT_TZ)
    for member in voice_channel.members:
        if member.bot:
            continue
        
        db_join_time = get_user_join_timestamp(member.id)
        start_time = user_last_proof.get(member.id) or user_join_time.get(member.id) or db_join_time or now
        elapsed = (now - start_time).total_seconds() / 60

        db_remaining = get_or_reset_user_remaining_minutes(member.id)
        warning_threshold = max(0, db_remaining - 5)

        if warning_threshold <= elapsed < db_remaining:
            if member.id not in warned_users:
                warned_users.add(member.id)
                remaining_mins = int(db_remaining - elapsed)
                if study_channel:
                    await study_channel.send(f"⚠️ {member.mention} باقي لك حوالي **{remaining_mins} دقائق** وتتطرد من الروم! اسرع وابعث صورة المذاكرة 📸")

        if elapsed > db_remaining:
            try:
                await member.move_to(None)
                warned_users.discard(member.id)
                user_join_time.pop(member.id, None)
                user_last_proof.pop(member.id, None)
                set_user_join_timestamp(member.id, None)
                
                set_user_remaining_minutes(member.id, 0)

                if study_channel:
                    await study_channel.send(f"🚨 {member.mention} تم إخراجك من روم المذاكرة الصوتية لعدم إرسال إثبات المذاكرة خلال مهلتك المحددة!")
            except Exception as e:
                print(f"حدث خطأ أثناء نقل العضو {member}: {e}")

token = os.getenv("DISCORD_BOT_TOKEN")
if not token:
    token = getpass.getpass("Enter Discord bot token: ")
if not token:
    raise RuntimeError("A Discord bot token is required to start the bot.")

bot.run(token)
