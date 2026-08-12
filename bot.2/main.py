import discord
from discord.ext import commands
import random
import asyncio
import aiosqlite
import json

# Requer: pip install discord.py aiosqlite

TOKEN = "MTQ3MzY2NDIwMzE3OTk1MDE1MQ.GNpaFa.JYcGGIukgVSDhb7dz_MwJSZrDBjqYtuSOhjtI0"  # Coloque seu token aqui

intents = discord.Intents.default()
intents.message_content = True

DB_FILE = "xp_data.db"


class RPGBot(commands.Bot):
    async def setup_hook(self):
        # setup_hook roda antes do bot conectar ao Discord, então a conexão
        # com o banco já está pronta quando as mensagens começarem a chegar.
        self.db = await aiosqlite.connect(DB_FILE)

        # WAL deixa leituras e escritas concorrentes mais rápidas, e
        # synchronous=NORMAL reduz o custo de cada commit sem abrir mão
        # de segurança relevante para esse tipo de uso.
        await self.db.execute("PRAGMA journal_mode=WAL")
        await self.db.execute("PRAGMA synchronous=NORMAL")

        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id INTEGER PRIMARY KEY,
                xp_per_level INTEGER NOT NULL DEFAULT 100,
                xp_channel INTEGER,
                level_channel INTEGER
            )
        """)
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                xp INTEGER NOT NULL DEFAULT 0,
                level INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (guild_id, user_id)
            )
        """)
        await self.db.commit()

    async def close(self):
        if hasattr(self, "db"):
            await self.db.close()
        await super().close()


bot = RPGBot(command_prefix="?", intents=intents)

# =========================
# SISTEMA DE DADOS (SQLite)
# =========================

async def ensure_guild_settings(guild_id: int):
    """Cria a linha da guild em guild_settings com valores padrão, se ainda não existir."""
    await bot.db.execute(
        "INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)",
        (guild_id,)
    )
    await bot.db.commit()


async def get_or_create_guild_settings(guild_id: int):
    await ensure_guild_settings(guild_id)
    async with bot.db.execute(
        "SELECT xp_per_level, xp_channel, level_channel FROM guild_settings WHERE guild_id = ?",
        (guild_id,)
    ) as cursor:
        row = await cursor.fetchone()
    return {"xp_per_level": row[0], "xp_channel": row[1], "level_channel": row[2]}


async def get_or_create_user(guild_id: int, user_id: int):
    """Retorna xp/level do usuário, criando um registro (xp=0, level=1) se ele ainda não existir."""
    await bot.db.execute(
        "INSERT OR IGNORE INTO users (guild_id, user_id) VALUES (?, ?)",
        (guild_id, user_id)
    )
    await bot.db.commit()
    async with bot.db.execute(
        "SELECT xp, level FROM users WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id)
    ) as cursor:
        row = await cursor.fetchone()
    return {"xp": row[0], "level": row[1]}


async def fetch_user(guild_id: int, user_id: int):
    """Retorna xp/level do usuário ou None se ele ainda não tiver XP registrado (não cria linha)."""
    async with bot.db.execute(
        "SELECT xp, level FROM users WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id)
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return None
    return {"xp": row[0], "level": row[1]}


async def save_user(guild_id: int, user_id: int, xp: int, level: int):
    """Grava xp/level do usuário (insere se for a primeira vez, atualiza se já existir)."""
    await bot.db.execute(
        """
        INSERT INTO users (guild_id, user_id, xp, level) VALUES (?, ?, ?, ?)
        ON CONFLICT(guild_id, user_id) DO UPDATE SET xp = excluded.xp, level = excluded.level
        """,
        (guild_id, user_id, xp, level)
    )
    await bot.db.commit()

# =========================
# HP
# ========================


# =========================
# BARRA DE XP BONITA
# =========================

def create_xp_bar(current_xp, xp_needed):
    total_blocks = 16
    progress_ratio = current_xp / xp_needed
    filled_blocks = int(progress_ratio * total_blocks)
    empty_blocks = total_blocks - filled_blocks
    bar = "🟩" * filled_blocks + "⬜" * empty_blocks
    percent = round(progress_ratio * 100, 1)
    remaining = xp_needed - current_xp
    return bar, percent, remaining


# =========================
# SISTEMA DE XP POR CARACTERES
# =========================

@bot.event
async def on_message(message):
    if message.author.bot or not message.guild:
        return

    settings = await get_or_create_guild_settings(message.guild.id)

    if settings["xp_channel"] is None or message.channel.id != settings["xp_channel"]:
        await bot.process_commands(message)
        return

    if len(message.content) < 10:
        await bot.process_commands(message)
        return

    user_data = await get_or_create_user(message.guild.id, message.author.id)

    # XP ganho por caracteres
    xp_gain = len(message.content) // 5
    user_data["xp"] += xp_gain

    leveled_up = False

    while True:
        xp_needed = int(settings["xp_per_level"] * (1.2 ** (user_data["level"] - 1)))
        if user_data["xp"] < xp_needed:
            break
        user_data["xp"] -= xp_needed
        user_data["level"] += 1
        leveled_up = True

    await save_user(message.guild.id, message.author.id, user_data["xp"], user_data["level"])

    if leveled_up:
        bar, percent, remaining = create_xp_bar(user_data["xp"], xp_needed)
        embed = discord.Embed(
            title="✨ LEVEL UP ✨",
            description=(
                f"{message.author.mention} alcançou o **Nível {user_data['level']}**!\n\n"
                f"📊 **Progresso:**\n{bar} {percent}%\n"
                f"🔹 XP: {user_data['xp']} / {xp_needed}\n"
                f"🔸 Faltam: {remaining} XP"
            ),
            color=0x00ff99
        )
        if settings["level_channel"]:
            channel = bot.get_channel(settings["level_channel"])
            if channel:
                await channel.send(embed=embed)
        else:
            await message.channel.send(embed=embed)

    await bot.process_commands(message)

# ========================
# Timer
# ========================
@bot.command(name="timer")
@commands.has_permissions(administrator=True)
async def timeresenha(ctx, minutos: int):
    if minutos <= 0:
        await ctx.send("❌ Coloque um número maior que 0.")
        return

    await ctx.send(
        f"⏳ Contagem iniciada! Vou avisar em **{minutos} minuto(s)**."
    )

    # espera o tempo
    await asyncio.sleep(minutos * 60)

    # avisa todo mundo
    await ctx.send(
        "@everyone Iniciando!👀"
    )
    # limpar
@bot.command(name="limpar")
@commands.has_permissions(administrator=True)
async def limpar(ctx):
    await ctx.channel.purge()
# ========================
# Resenha
# ========================
@bot.command(name="resenha")
async def resenha(ctx):
    # envia a mensagem inicial
    msg = await ctx.send("👀 Averiguando possível resenha...")

    # espera 2 segundos
    await asyncio.sleep(3)

    # sorteia resultado
    resultado = random.choice([
        "✅ Resenha encontrada!",
        "❌ Sem sinal de resenha."
    ])

    # edita a mesma mensagem
    await msg.edit(content=resultado)

# =========================
# Bolo ou pica
# =========================


# =========================
# COMANDOS ADMIN
# =========================

@bot.command()
@commands.has_permissions(administrator=True)
async def setxp(ctx, quantidade: int):
    await ensure_guild_settings(ctx.guild.id)
    await bot.db.execute(
        "UPDATE guild_settings SET xp_per_level = ? WHERE guild_id = ?",
        (quantidade, ctx.guild.id)
    )
    await bot.db.commit()
    await ctx.send(f"XP base necessário para upar definido como {quantidade}.")

@bot.command()
@commands.has_permissions(administrator=True)
async def setxpchannel(ctx, canal: discord.TextChannel):
    await ensure_guild_settings(ctx.guild.id)
    await bot.db.execute(
        "UPDATE guild_settings SET xp_channel = ? WHERE guild_id = ?",
        (canal.id, ctx.guild.id)
    )
    await bot.db.commit()
    await ctx.send(f"Canal de XP definido para {canal.mention}.")

@bot.command()
@commands.has_permissions(administrator=True)
async def setlevelchannel(ctx, canal: discord.TextChannel):
    await ensure_guild_settings(ctx.guild.id)
    await bot.db.execute(
        "UPDATE guild_settings SET level_channel = ? WHERE guild_id = ?",
        (canal.id, ctx.guild.id)
    )
    await bot.db.commit()
    await ctx.send(f"Canal de level up definido para {canal.mention}.")

@bot.command()
@commands.has_permissions(administrator=True)
async def addxp(ctx, membro: discord.Member, quantidade: int):
    user_data = await get_or_create_user(ctx.guild.id, membro.id)
    novo_xp = user_data["xp"] + quantidade
    await save_user(ctx.guild.id, membro.id, novo_xp, user_data["level"])
    await ctx.send(f"{quantidade} XP adicionados para {membro.mention}.")

@bot.command()
@commands.has_permissions(administrator=True)
async def removexp(ctx, membro: discord.Member, quantidade: int):
    user_data = await fetch_user(ctx.guild.id, membro.id)
    if user_data is None:
        await ctx.send("Usuário não possui XP.")
        return
    novo_xp = max(0, user_data["xp"] - quantidade)
    await save_user(ctx.guild.id, membro.id, novo_xp, user_data["level"])
    await ctx.send(f"{quantidade} XP removidos de {membro.mention}.")

@bot.command()
@commands.has_permissions(administrator=True)
async def setlevel(ctx, membro: discord.Member, nivel: int):
    user_data = await get_or_create_user(ctx.guild.id, membro.id)
    novo_nivel = max(1, nivel)
    await save_user(ctx.guild.id, membro.id, user_data["xp"], novo_nivel)
    await ctx.send(f"Nível de {membro.mention} definido para {novo_nivel}.")

# =========================
# COMANDO RANK
# =========================

@bot.command()
async def rank(ctx):
    settings = await get_or_create_guild_settings(ctx.guild.id)
    user_data = await fetch_user(ctx.guild.id, ctx.author.id)
    if user_data is None:
        await ctx.send("Você ainda não tem XP.")
        return
    xp_needed = int(settings["xp_per_level"] * (1.2 ** (user_data["level"] - 1)))
    bar, percent, remaining = create_xp_bar(user_data["xp"], xp_needed)
    embed = discord.Embed(
        title=f"🏆 Rank de {ctx.author.name}",
        description=(
            f"🎖️ **Nível:** {user_data['level']}\n\n"
            f"📊 **Progresso:**\n{bar} {percent}%\n"
            f"🔹 XP Atual: {user_data['xp']}\n"
            f"🔸 XP Necessário: {xp_needed}\n"
            f"✨ Faltam: {remaining} XP"
        ),
        color=0x3498db
    )
    await ctx.send(embed=embed)

# =========================
# COMANDOS DE AJUDA
# =========================

@bot.command()
async def ajuda(ctx):
    embed = discord.Embed(title="📜 Lista de Comandos", color=0x9b59b6)
    embed.add_field(
        name="👥 Públicos",
        value=(
            "`?rank`\n"
            "`?kashira`\n"
            "`?fragmentado`\n"
            "`?d20, ?d40, ?d60, ?d100`\n"
            "`?ajuda`"
        ),
        inline=False
    )
    await ctx.send(embed=embed)

@bot.command()
@commands.has_permissions(administrator=True)
async def morrakelvyn(ctx):
    embed = discord.Embed(title="🛠️ Comandos de Administrador", color=0xe74c3c)
    embed.add_field(
        name="⚙️ Administração de XP e Nível",
        value=(
            "`?setxp <quantidade>` - Define XP base para upar\n"
            "`?setxpchannel #canal` - Define canal que conta XP\n"
            "`?setlevelchannel #canal` - Define canal de level up\n"
            "`?addxp @usuario <quantidade>` - Adiciona XP\n"
            "`?removexp @usuario <quantidade>` - Remove XP\n"
            "`?setlevel @usuario <nivel>` - Define nível de um usuário"
        ),
        inline=False
    )
    await ctx.send(embed=embed)

# =========================
# COMANDOS RPG ORIGINAIS
# =========================

@bot.command(name="kashira")
async def kashira(ctx):
    opcoes = [
        "Transformação em boneco",
        "Técnica da marionete",
        "Técnica do voodoo"
    ]
    gifs = [
        "https://tenor.com/view/horror-houseofwax-gif-18765811",
        "https://tenor.com/view/puppet-fnaf-five-nights-at-freddys-gangnam-style-dance-gif-9941259697419559025",
        "https://tenor.com/view/nobara-nobara-kugisaki-kugisaki-nobara-jujutsu-kaisen-jjk-gif-9522241155096356986"
    ]
    index = random.randint(0, len(opcoes)-1)
    embed = discord.Embed(
        title="Resultado do Kashira!",
        description=f"Jogador tem: **{opcoes[index]}**",
        color=0x00ff00
    )
    embed.set_image(url=gifs[index])
    await ctx.send(embed=embed)

@bot.command(name="fragmentado")
async def fragmentado(ctx):
    opcoes = [
        "Deus da criação +30 em inteligencia",
        "Deus da crueldade +15 em magia",
        "Deus do mar +15 em agilidade e capacidade de nadar na terra",
        "Deus galinha +1 de força",
        "Deus dos peixes +10 de velocidade",
        "Deus das emoções +20 em inteligencia",
        "Deus dos ursos +10 em resistencia e força",
        "Deus das pernas +19 em velocidade",
        "Deus da inexistencia +30 em magia",
        "Deus dos braços +19 em força",
        "Deus da velhice +imortalidade vital",
        "Deus da violencia +25 de força"
    ]
    embed = discord.Embed(
        title="Resultado do Fragmentado!",
        description=f"Jogador é fragmentado do: **{random.choice(opcoes)}**",
        color=0xff0000
    )
    await ctx.send(embed=embed)

@bot.command(name="dom")
async def dom(ctx):
    opcoes = [
       "Criação",
       "Névoa",
       "Névoa",
       "Som",
       "Som",
       "Deslizar",
       "Deslizar",
       "Levitar",
       "Levitar",
       "Controle de açucar",
       "Acido",
       "Patinação",
       "Disparo de teia",
       "Fragmentação temporal",
       "Magnetismo",
       "Gigantismo",
       "Metamorfose",
       "Moldagem de corpos",
       "Explosão de umidade"
    ]
    embed = discord.Embed(
        title="Resultado do Dom!",
        description=f"Jogador tem o dom: **{random.choice(opcoes)}**",
        color=0xff0000
    )
    await ctx.send(embed=embed)

# =========================
# RAÇAS
# =========================
@bot.command(name="raça")
async def raça(ctx):
    opcoes = [
        "Hibrido",
        "Hibrido",
        "Hibrido",
        "Hibrido mistico",
        "Hibrido mistico",
        "Humanos",
        "Eienno",
        "Eienno",
        "Humano",
        "Humano",
        "Humano",
    ]
    embed = discord.Embed(
        title="Raça selecionada",
        description=f"Jogador tem a raça {random.choice(opcoes)}",
        color=0xff0000
        
    )
    await ctx.send(embed=embed)

# =========================
# Sub raça
# =========================
 
@bot.command(name="sub_raça")
async def sub_raça(ctx):
    opcoes = [
        "Temoni",
        "Temoni",
        "Temoni",
        "Solareth",
        "Solareth",
        "Solareth",
        "Fragmentado"
    ]
    embed = discord.Embed(
        title="Sub raça",
        description=f"A sub raça do player é {random.choice(opcoes)}",
        color=0xff0000
    )
    await ctx.send(embed=embed)
    
# =========================
# CLAN
# =========================
@bot.command(name="clan")
async def clan(ctx):
    opcoes = [
        "Yoshigune! (gire ?yoshigune)",
        "Soto! (gire ?soto)",
        "Fexa!",
        "Haraki! (gire ?haraki)",
        "Karyushi! (gire ?karyushi)",
        "Nhorans!",
        "Valkrins!",
        "Valriths!",
        "Rohan!",
        "Kashira! (gire ?kashira)",
        "Daruros!",
        "N!",
        "Clan comum, ponha qualquer sobrenome",
        "Clan comum, ponha qualquer sobrenome",
        "Clan comum, ponha qualquer sobrenome",
        "Clan comum, ponha qualquer sobrenome",
        "Clan comum, ponha qualquer sobrenome",
        "Clan comum, ponha qualquer sobrenome",
        "Clan comum, ponha qualquer sobrenome",
    ]
    embed = discord.Embed(
        title="Clan",
        description=f"Seu clan é {random.choice(opcoes)}",
        color=0xff0000
    )
    await ctx.send(embed=embed)
    
# =========================
# Yoshigune
# =========================
@bot.command(name="yoshigune")
async def yoshigune(ctx):
    opcoes = [
        "só é forte mesmo, nada especial",
        "só é forte mesmo, nada especial",
        "só é forte mesmo, nada especial",
        "é um solar",
        "é um lunar",
        "é um ceifador!"
        
    ]
    embed = discord.Embed(
        title="Yoshigune",
        description=f"Você {random.choice(opcoes)}",
        color=0xff0000
    )
    await ctx.send(embed=embed)

# =========================
# Haraki
# =========================
@bot.command(name="haraki")
async def haraki(ctx):
    opcoes = [
        "não é nada especial",
        "não é nada especial",
        "não é nada especial",
        "não é nada especial",
        "possui do dom da gravidade!",
        "é uma vida!"
    ]    
    embed = discord.Embed(
        title="Haraki",
        description=f"Você {random.choice(opcoes)}",
        color=0xff0000
    )
    await ctx.send(embed=embed)
    
# =========================
# karyushi
# =========================

@bot.command(name="karyushi")
async def karyushi(ctx):
    opcoes = [
        "é só mais uma pessoa normal do clan msm",
        "é só mais uma pessoa normal do clan msm",
        "é só mais uma pessoa normal do clan msm",
        "é o elemental das chamas! (automaticamente se tornando hibrido de raposa)"
    ]
    embed = discord.Embed(
        title="karyushi",
        description=f"Você {random.choice(opcoes)}",
        color=0xff00000
    )
    await ctx.send(embed=embed)
    
# ========================
# Soto
# ========================
@bot.command(name="soto")
async def soto(ctx):
    opcoes = [
        "é só forte mesmo",
        "é só forte mesmo",
        "é só forte mesmo",
        "é só forte mesmo",
        "possui o sacrificio!",
    ]
    embed = discord.Embed(
        title="Soto",
        description=f"Você {random.choice(opcoes)}",
        color=0xff00000
    )
    await ctx.send(embed=embed)
    
# =========================
# elemento
# =========================
@bot.command(name="elemento")
async def elemento(ctx):
    opcoes = [
        "água",
        "pedra",
        "vento",
        "luz",
        "escuridão",
        "trovão",
        "gelo",
        "natureza",
        "buraco negro"
    ]
    embed = discord.Embed(
        title="Elemento",
        description=f"Você possui o elemento {random.choice(opcoes)}",
        color=0xff0000
    )
    await ctx.send(embed=embed)
    
# =========================
# elemental
# =========================
@bot.command(name="elemental")
async def elemental(ctx):
    opcoes = [
        "Você não é o elemental!",
        "Você não é o elemental!",
        "Você não é o elemental!",
        "Você não é o elemental!",
        "Você não é o elemental!",
        "Você não é o elemental!",
        "Você é o elemental!"
    ]
    embed = discord.Embed(
        title="Elemental",
        description=f"{random.choice(opcoes)}",
        color=0xff0000
    )
    await ctx.send(embed=embed)
    
# =========================
# manipulação
# =========================
@bot.command(name="manipulação")
async def manipulaçao(ctx): 
    opcoes = [
        "calor",
        "eletricidade",
        "lixo",
        "sangue",
        "ossos"
    ]
    embed = discord.Embed(
        title="Manipulção",
        description=f"{random.choice(opcoes)}",
        color=0xff0000
    )
    await ctx.send(embed=embed)
    
# =========================
# BENÇÃO    
# =========================
@bot.command(name="benção")
async def bençao(ctx):
    opcoes = [
        "os olhos de Deus(basicos)",
        "os olhos de Deus(avançados)",
        "os olhos de Deus(despertados)",
        "as marcas de ardat",
        "visão purgatoria",
        "evolução voluntaria",
        "nenhuma"
        "nenhuma"
        "nenhuma"
        "nenhuma"
        "nenhuma"
        "nenhuma"
        "nenhuma"
        "nenhuma"
        "nenhuma"
        "nenhuma lx"
        
    ]
    embed = discord.Embed(
        title="Benção",
        description=f"Player possui {random.choice(opcoes)}",
        color=0xff0000
    )
    await ctx.send(embed=embed)

# =========================
# COMANDOS DADOS FIXOS
# =========================

@bot.command()
async def d20(ctx):
    resultado = random.randint(1, 20)
    embed = discord.Embed(
        title=f"🎲 {ctx.author.name} rolou 1d20",
        description=f"Resultado: **{resultado}**",
        color=0x1abc9c
    )
    await ctx.send(embed=embed)

@bot.command()
async def d40(ctx):
    resultado = random.randint(1, 40)
    embed = discord.Embed(
        title=f"🎲 {ctx.author.name} rolou 1d40",
        description=f"Resultado: **{resultado}**",
        color=0x1abc9c
    )
    await ctx.send(embed=embed)

@bot.command()
async def d60(ctx):
    resultado = random.randint(1, 60)
    embed = discord.Embed(
        title=f"🎲 {ctx.author.name} rolou 1d60",
        description=f"Resultado: **{resultado}**",
        color=0x1abc9c
    )
    await ctx.send(embed=embed)

@bot.command()
async def d100(ctx):
    resultado = random.randint(1, 100)
    embed = discord.Embed(
        title=f"🎲 {ctx.author.name} rolou 1d100",
        description=f"Resultado: **{resultado}**",
        color=0x1abc9c
    )
    await ctx.send(embed=embed)

# =========================
# BOT ONLINE
# =========================

@bot.event
async def on_ready():
    print(f"Conectado como {bot.user}")

bot.run(TOKEN)