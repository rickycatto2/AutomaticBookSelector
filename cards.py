"""Downloadable PNG card containing aggregate statistics only."""
import datetime
import io

from PIL import Image, ImageDraw, ImageFont


def card_png(stats):
    image = Image.new('RGB', (1080, 900), '#f8f5ed')
    draw = ImageDraw.Draw(image)
    def text(x, y, value, size, color='#1e493b'):
        draw.text((x, y), value, font=ImageFont.load_default(size=size), fill=color)
    def number(value, suffix=''):
        return 'Not available' if value is None else f'{value:.1f}{suffix}'
    text(70, 48, 'MY LISTENING MONTH', 24)
    label = datetime.date.fromisoformat(stats['month'] + '-01').strftime('%B %Y')
    text(70, 103, label, 60)
    entries = [(str(stats['completed']), 'BOOKS COMPLETED'), (number(stats['source_hours']), 'SOURCE HOURS'),
               (number(stats['actual_hours']), 'ACTUAL HOURS'), (number(stats['average_speed'], 'x'), 'AVERAGE SPEED'),
               (number(stats['hours_saved']), 'HOURS SAVED VS 1x'), (number(stats['maximum_sustained_speed'], 'x'), 'FASTEST SUSTAINED')]
    for i, (value, title) in enumerate(entries):
        x, y = 70 + i % 2 * 510, 220 + i // 2 * 155
        text(x, y, value, 48)
        text(x, y + 70, title, 20, '#6b756c')
    text(70, 750, 'AutomaticBookSelector - My shelf. My pace.', 19, '#6b756c')
    text(70, 797, f"{stats['measured_sessions']}/{stats['session_count']} sessions measured - America/Chicago - Sessions started in this month", 17, '#6b756c')
    text(70, 830, 'Listening totals include replays. Skipped audio is excluded. Private reviews are omitted.', 17, '#6b756c')
    output = io.BytesIO()
    image.save(output, format='PNG')
    return output.getvalue()
