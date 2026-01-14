#!/usr/bin/env python3
"""Fix alerts_test to respond immediately"""

with open('bot/cogs/alerts.py', 'r', encoding='utf-8') as f:
    content = f.read()

old = """            # Run the check
            await self.check_alerts(force=force)
            
            # Provide feedback
            await interaction.followup.send(
                f"✅ Alert check completed!\\n"
                f"• Force mode: {force}\\n"
                f"• Tickers: {len(self.tickers)}\\n"
                f"• Check `#alerts` and `#bot-logs` for results.",
                ephemeral=True
            )"""

new = """            # Respond immediately, then run check in background
            await interaction.followup.send(
                f"🔄 Starting alert check...\\n"
                f"• Force mode: {force}\\n"
                f"• Tickers: {len(self.tickers)}\\n"
                f"• This may take a moment. Check `#alerts` and `#bot-logs` for results.",
                ephemeral=True
            )
            
            # Run check in background task to avoid timeout
            import asyncio
            asyncio.create_task(self.check_alerts(force=force))"""

content = content.replace(old, new)

with open('bot/cogs/alerts.py', 'w', encoding='utf-8') as f:
    f.write(content)

print("Fixed!")
