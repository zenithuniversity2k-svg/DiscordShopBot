
import discord
from discord.ext import commands
from discord.ui import Button, View, Select
import os
import json
import asyncio
import stripe
from dotenv import load_dotenv
from pymongo import MongoClient
from quart import Quart, request, jsonify

# Load environment variables
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')
MONGO_URI = os.getenv('MONGO_URI')
STRIPE_SECRET = os.getenv('STRIPE_SECRET')   # Webhook Secret (whsec_...)
SELLAPP_SECRET = os.getenv('SELLAPP_SECRET') 
STRIPE_API_KEY = os.getenv('STRIPE_API_KEY') # API Key (sk_live_...)

# Configure Stripe
if STRIPE_API_KEY:
    stripe.api_key = STRIPE_API_KEY

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

# Quart Setup
app = Quart(__name__)

@app.route('/')
async def home():
    return "Bot is Online!"

@app.route('/webhook', methods=['POST'])
async def webhook():
    """
    Unified Webhook Listener:
    1. Stripe Webhooks (checkout.session.completed)
    2. SellApp Webhooks
    """
    payload = await request.get_data()
    sig_header = request.headers.get('Stripe-Signature')

    # --- STRIPE HANDLER ---
    if sig_header and STRIPE_SECRET:
        try:
            event = stripe.Webhook.construct_event(
                payload, sig_header, STRIPE_SECRET
            )
        except ValueError as e:
            return jsonify({"error": "Invalid payload"}), 400
        except stripe.error.SignatureVerificationError as e:
            return jsonify({"error": "Invalid signature"}), 400

        if event['type'] == 'checkout.session.completed':
            session = event['data']['object']
            
            # Get User ID and Product from Client Reference / Metadata
            user_id = session.get('client_reference_id')
            # Assuming product name is passed in metadata or we look up by price
            # For simplicity, let's use metadata: {'product_name': 'VIP'}
            metadata = session.get('metadata', {})
            product_name = metadata.get('product_name')

            if user_id and product_name:
                print(f"✅ Stripe Payment: {product_name} for User {user_id}")
                # Since we are in the same loop, we can just await the function if it was async,
                # but give_role_async is designed to be thread-safe for old flask.
                # Here we can just call it directly if we ensure it's async.
                await give_role_async(user_id, product_name)
        
        return jsonify({"status": "success"}), 200

    # --- SELLAPP / CUSTOM HANDLER ---
    data = await request.get_json()
    if not data:
        return jsonify({"error": "No data"}), 400
    
    secret_received = data.get('secret')
    if SELLAPP_SECRET and secret_received == SELLAPP_SECRET:
        product_name = data.get('product_name')
        user_id = data.get('discord_user_id')
        
        if product_name and user_id:
            print(f"✅ SellApp Payment: {product_name} for User {user_id}")
            await give_role_async(user_id, product_name)
            return jsonify({"status": "success"}), 200

    return jsonify({"error": "Unauthorized"}), 403

async def give_role_async(user_id, product_name):
    # Find Product Role
    products = get_all_products()
    if product_name not in products:
        print(f"Webhook Error: Product {product_name} not found.")
        return

    role_id = products[product_name]['role_id']
    
    # Find Guild (Assuming bot is in 1 main guild, or you pass guild_id in webhook)
    # For now, we iterate guilds to find the user
    for guild in bot.guilds:
        member = guild.get_member(int(user_id))
        if member:
            role = guild.get_role(role_id)
            if role:
                try:
                    await member.add_roles(role)
                    try:
                        await member.send(f"🎉 Payment Received! You have been given the **{role.name}** role.")
                    except:
                        pass # DM closed
                    print(f"Given role {role.name} to {member.name}")
                except Exception as e:
                    print(f"Failed to give role: {e}")
            break

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
        
        # Check if we have ANY payment method (Manual OR Auto)
        has_stripe_api = bool(STRIPE_API_KEY)
        has_manual_links = bool(final_payments)
        
        if not has_stripe_api and not has_manual_links:
            await interaction.response.send_message("❌ No payment methods configured! Contact Admin.", ephemeral=True)
            return

        view = PaymentView(product_name, product['price'], final_payments)
        await interaction.response.send_message(f"You selected **{product_name}** (${product['price']}).\nChoose a payment method:", view=view, ephemeral=True)

class PaymentView(View):
    def __init__(self, product_name, price, payments):
        super().__init__()
        self.product_name = product_name
        self.price = price
        
        # Check for Stripe API Integration
        if STRIPE_API_KEY:
            self.add_item(StripeCheckoutButton(product_name, price))

        for method, link in payments.items():
            # Skip manual stripe link if API is active
            if method.lower() == 'stripe' and STRIPE_API_KEY:
                continue
            self.add_item(Button(label=f"Pay with {method}", url=link, style=discord.ButtonStyle.link))
        
        # Add "I Paid" Button (Manual Fallback)
        self.add_item(PaidButton(product_name))

class StripeCheckoutButton(Button):
    def __init__(self, product_name, price):
        super().__init__(label="💳 Pay with Stripe (Auto)", style=discord.ButtonStyle.primary)
        self.product_name = product_name
        self.price = price # String "10.00"

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        
        try:
            # Convert price string to cents (e.g. "10.00" -> 1000)
            amount_cents = int(float(self.price) * 100)
            
            session = stripe.checkout.Session.create(
                payment_method_types=['card', 'cashapp'],
                line_items=[{
                    'price_data': {
                        'currency': 'usd',
                        'product_data': {'name': self.product_name},
                        'unit_amount': amount_cents,
                    },
                    'quantity': 1,
                }],
                mode='payment',
                client_reference_id=str(interaction.user.id), # PASS USER ID HERE
                metadata={'product_name': self.product_name}, # PASS PRODUCT NAME HERE
                success_url='https://discord.com/channels/@me', # Redirect back to Discord
                cancel_url='https://discord.com/channels/@me',
            )
            
            await interaction.followup.send(f"Click here to pay securely: {session.url}", ephemeral=True)
            
        except Exception as e:
            await interaction.followup.send(f"❌ Error creating payment session: {str(e)}", ephemeral=True)

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
            await asyncio.sleep(5)
            await interaction.channel.delete()
        else:
            await interaction.response.send_message("❌ Error: Role or Member not found.", ephemeral=True)

    @discord.ui.button(label="⛔ Deny & Close", style=discord.ButtonStyle.danger, custom_id="deny_close")
    async def deny(self, interaction: discord.Interaction, button: Button):
        if not interaction.user.guild_permissions.administrator:
            return
        await interaction.channel.delete()

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

async def main():
    async with bot:
        # Start Quart in the background
        bot.loop.create_task(app.run_task(host='0.0.0.0', port=int(os.environ.get("PORT", 5000))))
        await bot.start(TOKEN)

if __name__ == '__main__':
    if TOKEN:
        try:
            asyncio.run(main())
        except KeyboardInterrupt:
            # Handle Ctrl+C gracefully
            pass
    else:
        print("Error: DISCORD_TOKEN not found")
