# EdgeLab capstone walkthrough script

The screen cues refer to the extended local capstone setup with saved runs, a candidate leaderboard, and the Cointegration Test console enabled. The capstone report pins its reproducible baseline at `dfacf658` and cites the November 23 public update at `b71f01e`.

## 1. What is pairs trading, and why test for cointegration?

*Show **Overview** with SPY / QQQ selected.*

Imagine two ETFs that often rise and fall together. For pairs trading, I want to know something more specific. When their prices move apart, does the gap tend to return toward its usual level? Cointegration tests for that kind of long-run relationship after accounting for how strongly one price moves relative to the other.

A pairs trade buys the asset that looks cheap relative to the other and shorts the one that looks expensive. Shorting means borrowing shares, selling them, and later buying them back. The trade aims to profit when the gap closes. If the gap keeps widening, it can lose money. EdgeLab helps me investigate whether the relationship is strong enough to keep researching.

## 2. See what the agents and LangGraph do

*Show **Agent coordination** for the saved SPY / QQQ run. Then show slide 5 and open **Settings → Planner advice**.*

CQF and Fitch research informed the statistical protocol, and the CMU checkpoints shaped how we divided the work among the agents.

**LangGraph is the workflow engine.** It moves the research from planning to human approval, then runs each approved test in order. A failed check can end the run before the next test starts.

For this saved run, the **Planner Agent** offered AIC and BIC. These rules help choose how many past observations, or lags, to include. BIC penalizes extra lags more heavily in a sample like this. The researcher approved BIC before the tests ran, and the **Controller Agent** ran that exact protocol.

For new plans, **Ornith 1.5 35B A3B Q4_K_M** is an optional local adviser. In Settings, I choose **LM Studio**, click **Refresh models**, and select the loaded Ornith instance. **Test model** asks for one choice on a fixed synthetic prompt.

EdgeLab sends temperature **0.6**, Top P **0.95**, and Top K **20**. Temperature allows some variation, and Top P and Top K narrow the tokens Ornith can choose from. EdgeLab caps the reply at **128 tokens**, turns reasoning, streaming, and chat storage off, and checks for one allowed JSON choice. LM Studio manages Min P, repeat penalty, context length, and GPU offload.

## 3. Read the evidence and understand the Stop

*Return to SPY / QQQ. Show **Evidence**, then **Run trace**.*

Here, A is SPY and B is QQQ. Engle–Granger fits a relationship between their prices and tests whether the gap left over tends to return toward a usual level.

A p-value tells me how unusual a result this strong would be if there were no cointegration. We set the cutoff at **0.05** in advance and tested both directions. Fitting SPY on QQQ gave **0.134**. Fitting QQQ on SPY gave **0.160**.

Both values exceeded the cutoff. The **Critic Agent** applied the **Stop** rule, so the run ended before stability testing. In **Run trace**, I can see where it stopped and which evidence the **Reporter Agent** cited.

## 4. Prepare a new test and check for a break

*Show **Pairs**, then **Cointegration Test → Test a pair**. Show the synthetic approval, **Report ready**, and the saved **Continue** result.*

I use **Pairs** to find candidates. In this view, HST and USB appear first because the display groups **Continue** results at the top. Their screening rank is tenth, and their p-value after adjusting for all the pairs tested was **1.000**. They fell short of the screen’s threshold.

For a fresh demonstration, I’ll use **QAA and QBB**, a synthetic pair. I choose their frozen history, click **Prepare plan**, and review the proposed lag policy and full protocol. The digest is like a fingerprint that ties my approval to the exact test I reviewed. Then I click **Approve protocol and run**.

When the console says **Report ready**, I click **Refresh** and select the new run. Both Engle–Granger p-values were below **0.05**, so the **Critic Agent** moved on to CUSUM. CUSUM tracks the running sum of the model’s errors. Errors above and below zero can offset each other. If they keep leaning in one direction, the sum can cross a threshold and flag a possible break. Here, CUSUM detected no break, and the report showed **Continue** for further research.

## 5. Explain CrewAI and close with the trading takeaway

*Show the **evaluation slide**, then return to the **SPY / QQQ Stop**.*

**CrewAI organizes the optional advisory work.** It gives the Planner, Critic, and Reporter distinct jobs and instructions. I tested whether splitting those jobs improved the advice across **24 blinded synthetic cases**. Within each run, both arms used the same model, call budget, and temperature of **zero**. That fixed setting let me compare the two advisory setups.

With **Qwen**, the single agent scored **28.13%** and CrewAI scored **30.21%**. With **Ornith**, they scored **56.25%** and **58.33%**. CrewAI gained **2.08 percentage points** in each run, below the **10-point gate**. With Ornith, CrewAI got one more evidence check and one more reproducibility check right. Both arms were equally accurate on verdicts and flaw detection. I kept the deterministic rules in charge of the verdict.

**So would I short SPY and go long QQQ based on this run? I’d pass.** The pair missed the cointegration cutoff, and the market holdout stayed locked. Before putting money behind a pairs trade, I’d want it to hold up on fresh data and still make sense after spreads, fees, and share-borrowing costs. This run gave me a clear reason to sit it out.
