# Supplier credit-risk screening

## What a good summary is for this team, and why

A buyer should be able to read the brief and decide whether a supplier needs a closer look. I put the screening priority up front and explain what triggered it in plain language. The brief covers company performance, court cases and audit opinions where they matter, with industry figures when there’s a relevant match. Dates help the buyer tell a warning in an older audit from a more recent finding. The brief ends with something to verify, such as whether a winding-up petition is still pending.

## What you built

I use the company’s stated business activity to map it to a Malaysian Standard Industrial Classification (MSIC) code. Where there’s a suitable match, that code lets me add sector figures from the Department of Statistics Malaysia (DOSM).

I kept the financial calculations and screening rules in the backend. It calculates ratios and compares each statement with the previous one on file, marking whether the periods are consecutive. For example, the FY2022 and FY2024 statements for `SYN-000286` support a two-year comparison. The backend checks thresholds using unrounded figures and records which rules set the priority. ERNIE works from those results, which leaves less room for arithmetic errors in the summary.

The backend combines the financial, legal and audit findings and assigns the first priority that applies in the order below.

- `Urgent` applies when the company is recorded as winding up, has a defendant-side winding-up petition recorded as pending, or has a recorded defendant-side winding-up judgment. These triggers take precedence regardless of profitability.
- `Insufficient financial data` applies when the record has no financial statements and there’s no `Urgent` trigger.
- `High` applies when any one of these conditions is met.
    - A positive Zmijewski X-Score calculated from the latest available statement. The backend uses it as a financial distress warning based on profitability, leverage and liquidity ratios.
    - Severe financial stress. The two latest statements must cover consecutive annual periods, and both must show a net loss and negative operating cash flow. The latest statement must also show either a current ratio below 1 or debt-service coverage below 1.
    - Severe legal exposure requires at least two non-winding-up defendant judgments and at least one non-winding-up defendant case recorded as pending. The backend adds up available claim amounts from both groups. That total must reach at least 10% of the latest statement’s revenue, which must be positive.
- `Review` applies when any one of these conditions is met.
    - Negative operating cash flow in the latest available financial year, meaning the company used cash in its operations.
    - A current ratio below 1 in the latest available financial year, meaning short-term assets fell short of short-term liabilities.
    - Debt-service coverage below 1 in the latest available financial year, meaning earnings fell short of scheduled debt servicing.
    - A recorded judgment in a case where the company was the defendant.
    - A defendant-side case recorded as pending.
    - A material audit exception in any available financial year, such as a disclaimer of opinion or material uncertainty about the company’s ability to continue operating.
- `Lower observed flags` applies when the company has financial statements and none of the rules above apply. The record can still carry other warnings.

The backend puts the results and supporting evidence in one record per company. I send all 50 records in a single OpenRouter request to Baidu ERNIE 5.5 Flash Preview (`baidu/ernie-5.5-flash-preview:free`). The prompt asks for a brief of 100 to 160 words.

I started with Gemini 3.5 Flash Lite and moved to OpenRouter after repeated rate limits. I tried NVIDIA Nemotron 3.5 Lightning and Nemotron 3 Ultra there before settling on ERNIE. I also saved a run from the ChatGPT interface as a reference.

I built a separate judge with its own prompt. It reviews a saved summary against the original company record and the backend evidence. For each inconsistency it finds, it points to the claim, gives the evidence and suggests a correction. The report gives a score of 2 when the judge finds no issues, 1 when it finds only minor issues and 0 when it finds a material issue. The script counts the words directly.

Each successful full run saves a separate JSONL version. The viewer shows the company record beside a selected summary and lets the reader compare two saved versions. I can run the judge again on any saved version without regenerating the summaries.

I used Codex to help fetch and parse the DOSM data and map business activities to MSIC codes. It also helped write the wrapper around the OpenRouter Python SDK, the scripts that read and save the JSONL files, and the judge script.

## How you know whether it's good

The current evidence export assigns 4 companies to `Urgent`, 7 to `High`, 13 to `Review`, 23 to `Lower observed flags` and 3 to `Insufficient financial data`. The ERNIE run returned a summary for all 50 companies and reproduced all 50 assigned priorities. The summaries ranged from 117 to 144 words, so every summary met the length target. [Evidence export](data/companies_llm_input.jsonl) · [Saved summaries](summaries/openrouter-baidu-ernie-5.5-flash-preview-free-reasoning-20261122T201207311888Z.jsonl)

I used ERNIE with reasoning enabled for both generation and judging, with a separate prompt for the judge. The judge gave 47 summaries a score of 2 and three a score of 1. It flagged no material issues. I checked the three findings against the records. For `SYN-000546`, `SYN-000783` and `SYN-001529`, operating cash flow became positive in FY2023 and free cash flow became positive in FY2024. All three summaries placed both changes in FY2024. [Judge report](build/summary-judgments/openrouter-baidu-ernie-5.5-flash-preview-free-reasoning-20261122T201207311888Z-20261122T201322312771Z.json)

I also found an issue the judge missed. It gave `SYN-000286` a 2 even though the summary says free cash flow “turned slightly negative in FY2024.” Free cash flow was RM171,072 in FY2022 and negative RM20,288 in FY2024. There’s no FY2023 statement, so the record leaves the year of the change unknown. [Evidence](data/companies_llm_input.jsonl#L8) · [Summary](summaries/openrouter-baidu-ernie-5.5-flash-preview-free-reasoning-20261122T201207311888Z.jsonl#L8) · [Judgment](build/summary-judgments/openrouter-baidu-ernie-5.5-flash-preview-free-reasoning-20261122T201207311888Z-20261122T201322312771Z.json#L56)

## What you'd do next, and how you'd keep improving it

I’d have the backend check consecutive statements and give ERNIE a turnaround year for each cash flow measure when the statements support one. With a gap, it would supply the figures and their dates. I’d keep the three cases the judge flagged and `SYN-000286` as test cases for future prompt or model changes.

I’d check a sample of the other summaries that received a score of 2, starting with winding-up cases and companies with gaps in their accounts or ratios close to a screening threshold. Those checks would let me compare saved versions on factual accuracy as well as word count.

I’d give reviewers two saved versions without telling them which model or prompt wrote each one. After each brief, I’d ask which concern they would follow up on and what they would check first. Their answers would show whether the briefs give them enough to start the review.
