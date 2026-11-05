# Meeting notes

## 2026-11-04 14:00 — Requirements meeting with Karn Singh

Attendees: Karn Singh (client), Niraj Pankhania, Tejdeep Pathipati, Nikita Ravi. Recording and transcript on file.

**What we heard.** Car insurance. Claims arrive daily, one at a time, by email or web form, carrying a policy number, an auto-generated claim ID, policyholder name, date submitted, and sometimes attachments. Today a reviewer checks each claim against the policy (about 15 minutes each), nothing is recorded, then it goes to an adjuster. Karn wants the deterministic checks done automatically, a short list of claims needing a human, and a record of what happened. Downstream of intake a claim goes to eligibility and amount verification; that is out of scope for this iteration, but the data model should carry the fields the next step needs. Leadership wants to see both what passed and what was flagged. We can define the data shape ourselves using synthetic data.

**What we decided.** Flag-and-report only; approval and denial confirmed out of scope ("you can lay it up in your plan"). Two intake outcomes: Ready for adjuster, Needs review. History shows every claim and its outcome, not just flags. Claimed amount captured and shown, not verified. We send Karn the claim shape and the pass-vs-review scenario list for approval (sent 4 Nov evening). Demo: Wed 11 Nov 14:00.

**Questions asked by.** Niraj: current process, scope of outcome, approval out of scope, demo slot. Nikita: input format and arrival pattern, what happens to a good claim, other end users. Tejdeep: [fill in].

---
