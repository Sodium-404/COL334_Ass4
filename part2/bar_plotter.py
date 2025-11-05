import pandas as pd
import matplotlib.pyplot as plt
import numpy as np

# === Load your data ===
# Example CSV format:
# condition,link_util,jfi
# light,0.68,0.99
# medium,0.52,0.97
# heavy,0.34,0.91

df = pd.read_csv("bu.csv")

# === Set up bar positions ===
conditions = ["light", "medium", "heavy"]
x = np.arange(len(conditions))
width = 0.35  # bar width

# === Create the plot ===
fig, ax = plt.subplots(figsize=(8, 6))
bars1 = ax.bar(x - width/2, df["link_util"], width, label="Link Utilization", color='tab:blue')
bars2 = ax.bar(x + width/2, df["jfi"], width, label="Jain Fairness Index (JFI)", color='tab:orange')

# === Labeling ===
ax.set_xlabel("Background UDP Load Condition")
ax.set_ylabel("Metric Value")
ax.set_title("Effect of Bursty UDP Background Traffic on Fairness and Efficiency")
ax.set_xticks(x)
ax.set_xticklabels(conditions)
ax.set_ylim(0, 1.1)
ax.legend()
ax.grid(axis='y', linestyle='--', alpha=0.6)

# === Annotate bar values ===
for bars in [bars1, bars2]:
    for bar in bars:
        height = bar.get_height()
        ax.annotate(f"{height:.2f}",
                    xy=(bar.get_x() + bar.get_width()/2, height),
                    xytext=(0, 3),  # small offset
                    textcoords="offset points",
                    ha='center', va='bottom', fontsize=9)

plt.tight_layout()
plt.savefig("bu_impact_plot.png")
plt.show()
