import pandas as pd
import matplotlib.pyplot as plt

# Load your data
df = pd.read_csv("bu.csv")

# Compute averages if multiple iterations per bandwidth exist
bw_stats = df.groupby("udp_off_mean").agg({
    "jfi": "mean",
    "link_util": "mean"
}).reset_index()

# --- Plot both JFI and Link Utilization ---
fig, ax1 = plt.subplots(figsize=(9, 6))

# Plot JFI (left axis)
ax1.plot(bw_stats["udp_off_mean"], bw_stats["jfi"], 'o-', color='tab:blue', label='Jain Fairness Index (JFI)')
ax1.set_xlabel("Bottleneck Bandwidth (Mbps)")
ax1.set_ylabel("Jain Fairness Index", color='tab:blue')
ax1.tick_params(axis='y', labelcolor='tab:blue')
ax1.set_ylim(0.9, 1.05)

# Twin axis for Link Utilization
ax2 = ax1.twinx()
ax2.plot(bw_stats["udp_off_mean"], bw_stats["link_util"], 's--', color='tab:red', label='Link Utilization',)
ax2.set_ylabel("Link Utilization", color='tab:red')
ax2.tick_params(axis='y', labelcolor='tab:red')
ax2.set_ylim(0, 1.0)

# Title and grid
plt.title("Effect of Bottleneck Bandwidth on JFI and Link Utilization")
ax1.grid(True, linestyle='--', alpha=0.6)

# Combine legends
lines_1, labels_1 = ax1.get_legend_handles_labels()
lines_2, labels_2 = ax2.get_legend_handles_labels()
ax1.legend(lines_1 +lines_2, labels_1 +labels_2, loc='lower right')

plt.tight_layout()
plt.savefig("asym_jfi_linkutil_plot.png")
plt.show()

# --- Print summary stats ---
print("=== Bandwidth vs JFI & Link Utilization ===")
print(bw_stats.round(4))
