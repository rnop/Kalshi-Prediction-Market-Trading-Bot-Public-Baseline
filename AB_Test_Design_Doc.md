### A/B Test Design Overview
This document outlines the design, methodology, and evaluation framework for an A/B test comparing a Prediction Market Model against a Baseline Model across 15-minute BTC/ETH/XRP/SOL cryptocurrency UP/DOWN prediction markets.

The two models trade binary contracts (YES/NO) on whether the price of each asset will be UP or DOWN over a 15-minute window from the opening spot price.  

Performance is evaluated across two primary metrics:
- Mean P&L per contract
- Hit rate accuracy

The results of the A/B test will complement the results of Monte Carlo simulations stress testing trading the signal in practice. A/B test will determine if there is a statistically significant edge, while Monte Carlo allows us to visualize P&L distributions and drawdowns in realistic simulations.

### Hypothesis Tests
The purpose of this experiment is to determine whether incorporating a momentum signal derived using machine learning and lagged features improve the profitability and predictive accuracy of trades relative to a baseline strategy. 

**Hypothesis Test #1: Mean P&L Per Contract**

P&L (Profit and Loss) is used to measure the outcome of trades, showing whether the realized trade has gained or lost money over specific period. 

Determine whether the prediction model with momentum generates higher average P&L per contract than the baseline model without momentum:

$$\overline{\text{P\&L}} = \mu\_{\text{P\&L}} = \frac{1}{N}\sum\_{i=1}^{N}{\text{P\&L}\_i}$$

- Statistical Test: **Welch's t-Test for Two Means (One-Tailed)** 
	- Why? Student's t-test assumes equal variances. We cannot assume that the P&L variance of the prediction model is the same as the baseline model. In fact, we hope they are different because they're trading different sessions, entry prices, and momentums. Welch's t-test allows us to test two means with unequal variances. CLT helps with large samples.

$$H\_0: \mu\_{\text{model}} = \mu\_{\text{baseline}}$$

$$H\_A: \mu\_{\text{model}} > \mu\_{\text{baseline}}$$

$$t = \frac{\overline{x}\_{\text{model}} - \overline{x}\_{\text{baseline}}}{\sqrt{\frac{s^2\_{\text{model}}}{n\_{\text{model}}} + \frac{s^2\_{\text{baseline}}}{n\_{\text{baseline}}}}}$$

**Hypothesis Test #2: Hit Rate Accuracy**

Determine whether the prediction model with momentum predicts direction more accurately than the baseline model without momentum:

$$\text{Hit Rate} = \text{Accuracy} = \frac{\text{Number of correct predictions}}{\text{Total number of predictions (N)}}$$

- Statistical Test: **Two-Proportion Z-Test (One-Tailed)**
	- Why? Hit rate is a proportion and we're comparing two independent groups with a decent sample size. Z-test is more natural for a one-tailed test because we get a signed test statistic compared to a Chi-Squared Test that is always positive.

$$H\_0: p\_{\text{model}} = p\_{\text{baseline}}$$

$$H\_A: p\_{\text{model}} > p\_{\text{baseline}}$$

$$z = \frac{\hat{p}\_{\text{model}} - \hat{p}\_{\text{baseline}}}{\sqrt{\hat{p}(1 - \hat{p})\left(\frac{1}{n\_{\text{model}}} + \frac{1}{n\_{\text{baseline}}}\right)}} \quad \hat{p} \text{ is the pooled proportion}$$

**Why one-tailed?** Our hypothesis is directional because we're asking if our prediction model with momentum is better than the baseline model without momentum. One-tail concentrates all of the $\alpha$ in one tail, giving us more power to detect an effect. The tradeoff is that we can't determine if the prediction model is *significantly worse* which is acceptable (we just won't use the model). 

**Why two tests?** Hit rate and P&L should both be evaluated because a high hit rate does not translate to a positive P&L, and vice-versa. The hypothesis tests will allow us to evaluate profitability metrics and directional prediction accuracy together, which matters in practice.

**Multiple Comparisons?** We'll be running 10 tests total, 2 metrics across 4 assets, plus the total composite. We'll use Bonferroni correction to bring $\alpha$ down from $0.05/10$ to $0.005$ per test. 

### Experimental Design & Data Collection
Both models will enter a trade if all of the following conditions are met:
- YES or NO side of a contract is priced between 0.80 and 0.90.
- Entry window is the final 5 minutes of the 15-minute chain, excluding the final minute.
- Maximum 1 trade per asset per chain (1 for BTC/ETH/XRP/SOL, max 4 per 15-minute session.
- Same position sizing per trade (e.g. 10 contracts).
- Hold until resolution.

**Model A: Baseline**
- Enters trade on the first *non-momentum* signal, as long as all conditions are met. If the momentum signal is *not* fired, a trade is entered.

**Model B: Prediction Market Model**
- Enters trade on the first *momentum* signal, as long as all conditions are met. if the momentum is fired, a trade is entered.

For example, if preceding conditions are met, we compare the current momentum signal against the determined momentum threshold (e.g. <0.05). If the momentum signal is below, the baseline model trades. If the momentum signal is above, the prediction market model trades.

The following information will be recorded for every trade:
- Timestamp (**obfuscated to avoid reverse-engineering signal**)
- Session ID (**obfuscated to avoid reverse-engineering signal**)
- Regime using K-Means (**obfuscated to avoid reverse-engineering signal**)
- Asset (BTC/ETH/XRP/SOL)
- Entry price
- Direction Side (YES/NO)
- Momentum signal value 
- Model traded (Prediction/Baseline)
- Resolution Outcome (WIN/LOSE)
- P&L Per Contract

### Sample Size Calculation
Define the following statistical terms:
- $\alpha = 0.05$ ( $z\_{1-\alpha/2} = 1.96$ )
- $\text{Power} = 80\%$ ( $z\_{\text{power}} = 0.84$ )
- $\text{Desired Hit Rate Lift} = +2\%$
- $\text{Desired Mean P\&L Lift} = +0.05\%$

Sample size for a Two-Proportion Z-Test (with +2% lift):

$$n = \frac{\left(z\_{1-\alpha/2} \sqrt{2p(1-p)} + z\_{\text{power}}\sqrt{p\_1(1-p\_1) + p\_2(1-p\_2)}\right)^2}{(p\_2 - p\_1)^2} = 1{,}650 \quad \text{trades per group}$$

Sample size for a Welch's t-Test for Two Means (with +0.05 lift):

$$n = \frac{(s^2\_1 + s^2\_2)(z\_{1-\alpha/2} + z\_{\text{power}})^2}{\delta^2} = 2{,}628 \quad \text{trades per group}$$

### Metrics to Report
- Mean P&L
- Hit Rate Accuracy
- Sharpe Ratio