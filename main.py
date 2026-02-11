import discord
from discord.ext import commands
from discord.ui import Button, View, Select
import os
import json
from dotenv import load_dotenv
from pymongo import MongoClient

# Load environment variables
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')
MONGO_URI = os.getenv('MONGO_URI')

# Database Setup
if not MONGO_URI:
    print("Warning: MONGO_URI not found. Data will not be saved.")
    db = None
else:
    try:
        cluster = MongoClient(MONGO_URI)
        db = cluster["DiscordShopBot"]
        products_col = db["products"]
        payments_col = db["payments"]
        print("Connected to MongoDB!")
    except Exception as e:
        print(f"Failed to connect to MongoDB: {e}")
        db = None

# Bot Setup
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix='!', intents=intents)

# Helpers (MongoDB Wrappers)
def get_all_products():
    if db is None: return {}
    # Convert cursor to dict {name: data}
    return {p['_id']: p for p in products_col.find()}

def save_product(name, data):
    if db is None: return
    # Use _id as the unique key (product name)
    data['_id'] = name
    products_col.replace_one({'_id': name}, data, upsert=True)

def delete_product_db(name):
    if db is None: return
    products_col.delete_one({'_id': name})

def get_all_payments():
    if db is None: return {}
    # Payments stored as single document with _id='global_payments'
    doc = payments_col.find_one({'_id': 'global_payments'})
    return doc['methods'] if doc else {}

def save_payment(method, link):
    if db is None: return
    payments_col.update_one(
        {'_id': 'global_payments'},
        {'$set': {f'methods.{method}': link}},
        upsert=True
    )

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user.name}')
    print("Store Bot is Ready!")

# --- ADMIN SETUP COMMANDS ---

@bot.command(name='addproduct')
@commands.has_permissions(administrator=True)
async def add_product(ctx, name: str, price: str, role: discord.Role):
    """Add a product to the store. Usage: !addproduct "VIP" "10.00" @Role"""
    data = {'price': price, 'role_id': role.id, 'role_name': role.name, 'links': {}}
    save_product(name, data)
    await ctx.send(f'✅ Product **{name}** added for **${price}**. Reward: {role.mention}')

@bot.command(name='listproducts')
@commands.has_permissions(administrator=True)
async def list_products(ctx):
    """List all configured products and their details."""
    products = get_all_products()
    if not products:
        await ctx.send("Store is empty!")
        return
    
    embed = discord.Embed(title="📦 Configured Products", color=discord.Color.orange())
    for name, data in products.items():
        links = data.get('links', {})
        link_text = "\n".join([f"• {k}: <{v}>" for k, v in links.items()]) if links else "Using Global Payments"
        
        embed.add_field(
            name=f"🏷️ {name}", 
            value=f"**Price:** ${data['price']}\n**Role:** <@&{data['role_id']}>\n**Links:**\n{link_text}", 
            inline=False
        )
    await ctx.send(embed=embed)

@bot.command(name='delproduct')
@commands.has_permissions(administrator=True)
async def del_product(ctx, name: str):
    delete_product_db(name)
    await ctx.send(f'🗑️ Product **{name}** deleted.')

@bot.command(name='setpayment')
@commands.has_permissions(administrator=True)
async def set_payment(ctx, method: str, link: str):
    """Set GLOBAL payment links. Usage: !setpayment "PayPal" "paypal.me/link" """
    save_payment(method, link)
    await ctx.send(f'💰 Global Payment method **{method}** set to: <{link}>')

@bot.command(name='linkproduct')
@commands.has_permissions(administrator=True)
async def link_product(ctx, product_name: str, method: str, link: str):
    """Set SPECIFIC payment link for a product. Usage: !linkproduct "VIP" "Stripe" "https://..." """
    products = get_all_products()
    if product_name not in products:
        await ctx.send("❌ Product not found.")
        return
    
    # Update specific product
    if db:
        products_col.update_one(
            {'_id': product_name},
            {'$set': {f'links.{method}': link}}
        )
    await ctx.send(f'🔗 Linked **{method}** for **{product_name}** to: <{link}>')

# --- USER BUY FLOW (VIEWS) ---

class ProductSelect(Select):
    def __init__(self, products):
        options = [
            discord.SelectOption(label=name, description=f"Price: ${data['price']} - Role: {data['role_name']}")
            for name, data in products.items()
        ]
        super().__init__(placeholder="Select a product to buy...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        product_name = self.values[0]
        products = get_all_products()
        product = products[product_name]
        
        # Create Payment View
        global_payments = get_all_payments()
        specific_links = product.get('links', {})
        
        # Merge payments: Specific overrides Global
        final_payments = global_payments.copy()
        final_payments.update(specific_links)
        
        if not final_payments:
            await interaction.response.send_message("❌ No payment methods configured! Contact Admin.", ephemeral=True)
            return

        view = PaymentView(product_name, product['price'], final_payments)
        await interaction.response.send_message(f"You selected **{product_name}** (${product['price']}).\nChoose a payment method:", view=view, ephemeral=True)

class PaymentView(View):
    def __init__(self, product_name, price, payments):
        super().__init__()
        self.product_name = product_name
        self.price = price
        
        for method, link in payments.items():
            self.add_item(Button(label=f"Pay with {method}", url=link, style=discord.ButtonStyle.link))
        
        # Add "I Paid" Button
        self.add_item(PaidButton(product_name))

class PaidButton(Button):
    def __init__(self, product_name):
        super().__init__(label="✅ I Have Paid", style=discord.ButtonStyle.success)
        self.product_name = product_name

    async def callback(self, interaction: discord.Interaction):
        # Create Ticket Channel
        guild = interaction.guild
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            interaction.user: discord.PermissionOverwrite(read_messages=True, send_messages=True),
            # Add Admin Role overwrite here if needed
        }
        
        category = discord.utils.get(guild.categories, name="Orders")
        if not category:
            category = await guild.create_category("Orders")

        channel = await guild.create_text_channel(f"order-{interaction.user.name}", category=category, overwrites=overwrites)
        
        await interaction.response.send_message(f"🎉 Order created! Please go to {channel.mention}", ephemeral=True)
        
        # Send details to ticket
        embed = discord.Embed(title="New Order Created", description=f"User: {interaction.user.mention}\nProduct: **{self.product_name}**", color=discord.Color.green())
        embed.add_field(name="Instructions", value="Please upload a screenshot of your payment receipt here.\nAn Admin will verify and grant your role.")
        
        view = TicketAdminView(self.product_name, interaction.user.id)
        await channel.send(f"{interaction.user.mention} @here", embed=embed, view=view)

class TicketAdminView(View):
    def __init__(self, product_name, user_id):
        super().__init__(timeout=None)
        self.product_name = product_name
        self.user_id = user_id

    @discord.ui.button(label="👑 Approve & Give Role", style=discord.ButtonStyle.primary, custom_id="approve_role")
    async def approve(self, interaction: discord.Interaction, button: Button):
        # Check permissions
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ Only Admins can approve.", ephemeral=True)
            return

        products = get_all_products()
        if self.product_name not in products:
            await interaction.response.send_message("❌ Product no longer exists.", ephemeral=True)
            return

        role_id = products[self.product_name]['role_id']
        role = interaction.guild.get_role(role_id)
        member = interaction.guild.get_member(self.user_id)

        if role and member:
            await member.add_roles(role)
            await interaction.response.send_message(f"✅ Approved! {role.mention} given to {member.mention}.")
            await interaction.channel.send("Ticket will close in 5 seconds...")
            import asyncio
            await __import__('asyncio').sleep(5)
            await interaction.channel.delete()
        else:
            await interaction.response.send_message("❌ Error: Role or Member not found.", ephemeral=True)

    @discord.ui.button(label="⛔ Deny & Close", style=discord.ButtonStyle.danger, custom_id="deny_close")
    async def deny(self, interaction: discord.Interaction, button: Button):
        if not interaction.user.guild_permissions.administrator:
            return
        await interaction.channel.delete()

# --- MAIN COMMAND ---

@bot.command(name='store')
async def store(ctx):
    products = get_all_products()
    if not products:
        await ctx.send("Store is empty!")
        return
    
    view = View()
    view.add_item(ProductSelect(products))
    
    embed = discord.Embed(title="🛒 Server Store", description="Select a product below to purchase.", color=discord.Color.blue())
    await ctx.send(embed=embed, view=view)

if TOKEN:
    bot.run(TOKEN)
else:
    print("Error: DISCORD_TOKEN not found")
