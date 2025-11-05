import pandas as pd
import matplotlib.pyplot as plt
import numpy as np
from scipy import stats

# Load the CSVs
jitter_df = pd.read_csv("jitter.csv")
loss_df = pd.read_csv("loss.csv")

# Function to compute mean and 90% confidence interval
def mean_ci(series, confidence=0.90):
    n = len(series)
    mean = np.mean(series)
    sem = stats.sem(series)
    h = sem * stats.t.ppf((1 + confidence) / 2., n - 1)
    return mean, h

# Group by jitter and compute stats
jitter_stats = jitter_df.groupby("jitter")["ttc"].apply(mean_ci).apply(pd.Series)
jitter_stats.columns = ["mean", "ci"]

# Group by loss and compute stats
loss_stats = loss_df.groupby("loss")["ttc"].apply(mean_ci).apply(pd.Series)
loss_stats.columns = ["mean", "ci"]

# --- Plot: Jitter vs Download Time ---
plt.figure(figsize=(8, 6))
plt.errorbar(jitter_stats.index, jitter_stats["mean"], yerr=jitter_stats["ci"],
             fmt='-o', capsize=5, elinewidth=1.5)
plt.xlabel("Jitter (ms)")
plt.ylabel("Average Download Time (s)")
plt.title("Effect of Jitter on Download Time (90% CI)")
plt.grid(True, linestyle='--', alpha=0.6)
plt.tight_layout()
plt.savefig("jitter_plot.png")
plt.show()

# --- Plot: Loss vs Download Time ---
plt.figure(figsize=(8, 6))
plt.errorbar(loss_stats.index, loss_stats["mean"], yerr=loss_stats["ci"],
             fmt='-o', capsize=5, elinewidth=1.5)
plt.xlabel("Loss Rate (%)")
plt.ylabel("Average Download Time (s)")
plt.title("Effect of Loss Rate on Download Time (90% CI)")
plt.grid(True, linestyle='--', alpha=0.6)
plt.tight_layout()
plt.savefig("loss_plot.png")
plt.show()

# Print computed stats
print("\n=== Jitter Statistics (Mean + 90% CI) ===")
print(jitter_stats)

print("\n=== Loss Statistics (Mean + 90% CI) ===")
print(loss_stats)
