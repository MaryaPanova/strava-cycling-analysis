import pandas as pd
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import warnings
warnings.filterwarnings('ignore')

ORANGE = '#FC4C02'
BLUE   = '#2563EB'
GREEN  = '#16A34A'

df_raw = pd.read_csv('activities.csv')
df = df_raw[df_raw['Activity Type'].isin(['Virtual Ride', 'Ride'])].copy()
df['date'] = pd.to_datetime(df['Activity Date'], format='%b %d, %Y, %I:%M:%S %p', errors='coerce')
df = df.dropna(subset=['date']).sort_values('date').reset_index(drop=True)
df['distance_km'] = df['Distance']
df['speed_kmh']   = df['Average Speed'] * 3.6

monthly = df.groupby(df['date'].dt.to_period('M')).agg(
    total_km=('distance_km', 'sum'),
    avg_speed=('speed_kmh', 'mean'),
    avg_watts=('Average Watts', 'mean'),
).reset_index()
monthly['month_dt'] = monthly['date'].dt.to_timestamp()

INJURY_START   = pd.Timestamp('2025-10-01')
COMEBACK_START = pd.Timestamp('2025-12-01')

# Section 11: Comeback overview
def bar_color(dt):
    if dt < INJURY_START:     return ORANGE
    elif dt < COMEBACK_START: return '#D1D5DB'
    else:                     return BLUE

colors = [bar_color(d) for d in monthly['month_dt']]
fig, axes = plt.subplots(3, 1, figsize=(14, 11), sharex=True)

bars = axes[0].bar(monthly['month_dt'], monthly['total_km'], width=22,
                   color=colors, edgecolor='white')
axes[0].axvline(INJURY_START,   color='red',  lw=1.5, linestyle='--', alpha=0.6)
axes[0].axvline(COMEBACK_START, color=GREEN,  lw=1.5, linestyle='--', alpha=0.6)
axes[0].set_ylabel('km / month')
axes[0].set_title('Monthly Distance', fontweight='bold')
for bar, val in zip(bars, monthly['total_km']):
    if val > 50:
        axes[0].text(bar.get_x() + bar.get_width()/2, bar.get_height() + 10,
                     f'{val:.0f}', ha='center', va='bottom', fontsize=8)

axes[1].plot(monthly['month_dt'], monthly['avg_speed'], 'o-', color=ORANGE, lw=2)
axes[1].axvline(INJURY_START,   color='red',  lw=1.5, linestyle='--', alpha=0.6)
axes[1].axvline(COMEBACK_START, color=GREEN,  lw=1.5, linestyle='--', alpha=0.6)
axes[1].fill_between(monthly['month_dt'], monthly['avg_speed'], alpha=0.12, color=ORANGE)
axes[1].set_ylabel('km/h')
axes[1].set_title('Average Speed', fontweight='bold')

axes[2].plot(monthly['month_dt'], monthly['avg_watts'], 'o-', color=GREEN, lw=2)
axes[2].axvline(INJURY_START,   color='red',  lw=1.5, linestyle='--', alpha=0.6, label='Injury (Oct)')
axes[2].axvline(COMEBACK_START, color=GREEN,  lw=1.5, linestyle='--', alpha=0.6, label='Comeback (Dec)')
axes[2].fill_between(monthly['month_dt'], monthly['avg_watts'].ffill(), alpha=0.12, color=GREEN)
axes[2].set_ylabel('Watts')
axes[2].set_title('Average Power', fontweight='bold')
axes[2].legend()
axes[2].xaxis.set_major_formatter(mdates.DateFormatter('%b %Y'))
plt.xticks(rotation=30, ha='right')
fig.suptitle('Training Timeline: Before → Injury → Comeback',
             fontsize=15, fontweight='bold', y=1.01)
plt.tight_layout()
plt.savefig('comeback_overview.png', dpi=150, bbox_inches='tight')
print('Saved: comeback_overview.png')

pre  = monthly[monthly['month_dt'] < INJURY_START]
post = monthly[monthly['month_dt'] >= COMEBACK_START]
best = post.iloc[-2]
print('=' * 52)
print(f"  Pre-injury peak speed:  {pre['avg_speed'].max():.1f} km/h")
print(f"  Pre-injury peak power:  {pre['avg_watts'].max():.0f} W")
print(f"  Best comeback month ({best['date']}):")
print(f"    Speed: {best['avg_speed']:.1f} km/h  (+{best['avg_speed'] - pre['avg_speed'].max():.1f})")
print(f"    Power: {best['avg_watts']:.0f} W  (+{best['avg_watts'] - pre['avg_watts'].max():.0f})")
print('=' * 52)

# Section 12: Season forecast
post_full = monthly[(monthly['month_dt'] >= COMEBACK_START) &
                    (monthly['month_dt'] < '2026-05-01')].copy().reset_index(drop=True)
post_full['x'] = np.arange(len(post_full))

forecast_months = pd.date_range('2026-06-01', periods=4, freq='MS')
forecast_x      = np.arange(len(post_full), len(post_full) + 4)

spd_coeffs   = np.polyfit(np.log1p(post_full['x']), post_full['avg_speed'], 1)
spd_forecast = spd_coeffs[0] * np.log1p(forecast_x) + spd_coeffs[1]
spd_hist_fit = spd_coeffs[0] * np.log1p(post_full['x']) + spd_coeffs[1]

pwr_valid    = post_full.dropna(subset=['avg_watts'])
pwr_coeffs   = np.polyfit(np.log1p(pwr_valid['x']), pwr_valid['avg_watts'], 1)
pwr_forecast = pwr_coeffs[0] * np.log1p(forecast_x) + pwr_coeffs[1]
pwr_hist_fit = pwr_coeffs[0] * np.log1p(post_full['x']) + pwr_coeffs[1]

VOLUME_CAP  = monthly[monthly['month_dt'] < INJURY_START]['total_km'].max() * 1.3
km_coeffs   = np.polyfit(post_full['x'], post_full['total_km'], 1)
km_forecast = np.clip(km_coeffs[0] * forecast_x + km_coeffs[1], 0, VOLUME_CAP)

fig, axes = plt.subplots(3, 1, figsize=(13, 11), sharex=True)
hist_x = post_full['month_dt']

axes[0].plot(hist_x, post_full['avg_speed'], 'o-', color=ORANGE, lw=2, label='Actual')
axes[0].plot(hist_x, spd_hist_fit, '--', color=ORANGE, lw=1, alpha=0.5)
axes[0].plot(forecast_months, spd_forecast, 's--', color=ORANGE, lw=2, markersize=8, label='Forecast')
axes[0].fill_between(forecast_months, spd_forecast*0.93, spd_forecast*1.07, alpha=0.18, color=ORANGE)
for dt, val in zip(forecast_months, spd_forecast):
    axes[0].annotate(f'{val:.1f}', (dt, val), xytext=(0, 10),
                     textcoords='offset points', ha='center', fontsize=9,
                     color=ORANGE, fontweight='bold')
axes[0].set_ylabel('km/h')
axes[0].set_title('Speed Forecast', fontweight='bold')
axes[0].legend()

axes[1].plot(hist_x, post_full['avg_watts'], 'o-', color=GREEN, lw=2, label='Actual')
axes[1].plot(hist_x, pwr_hist_fit, '--', color=GREEN, lw=1, alpha=0.5)
axes[1].plot(forecast_months, pwr_forecast, 's--', color=GREEN, lw=2, markersize=8, label='Forecast')
axes[1].fill_between(forecast_months, pwr_forecast*0.93, pwr_forecast*1.07, alpha=0.18, color=GREEN)
for dt, val in zip(forecast_months, pwr_forecast):
    axes[1].annotate(f'{val:.0f}W', (dt, val), xytext=(0, 10),
                     textcoords='offset points', ha='center', fontsize=9,
                     color=GREEN, fontweight='bold')
axes[1].set_ylabel('Watts')
axes[1].set_title('Power Forecast', fontweight='bold')
axes[1].legend()

axes[2].bar(hist_x, post_full['total_km'], width=22, color=BLUE, alpha=0.7, label='Actual')
axes[2].bar(forecast_months, km_forecast, width=22, color=BLUE, alpha=0.35,
            label='Forecast', edgecolor=BLUE, linewidth=1.5)
for dt, val in zip(forecast_months, km_forecast):
    axes[2].text(dt, val + 15, f'{val:.0f}', ha='center', fontsize=9,
                 color=BLUE, fontweight='bold')
axes[2].axhline(VOLUME_CAP, color='gray', lw=1.2, linestyle=':',
                label=f'Cap ({VOLUME_CAP:.0f} km)')
axes[2].set_ylabel('km / month')
axes[2].set_title('Volume Forecast', fontweight='bold')
axes[2].legend()
axes[2].xaxis.set_major_formatter(mdates.DateFormatter('%b %Y'))
plt.xticks(rotation=30, ha='right')
fig.suptitle('Season Forecast — Jun to Sep 2026  (±7% band)',
             fontsize=14, fontweight='bold', y=1.01)
plt.tight_layout()
plt.savefig('season_forecast.png', dpi=150, bbox_inches='tight')
print('Saved: season_forecast.png')

print(f"\n{'Month':<12} {'Speed':>10} {'Power':>10} {'Volume':>12}")
print('-' * 46)
for dt, spd, pwr, km in zip(forecast_months, spd_forecast, pwr_forecast, km_forecast):
    print(f"{dt.strftime('%b %Y'):<12} {spd:>9.1f} {pwr:>9.0f}W {km:>10.0f} km")
