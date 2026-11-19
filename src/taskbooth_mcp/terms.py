"""The Task Booth Standard Terms, each version fixed by the hash of its text.

v0 was published under the working name MakerMarket; v1 adds Cashu escrow, the arbiter's signature and the inspector.

A job's cover page names the version and hash in force when it was posted, and
that version never changes for the job (section 2.5). A new version is a new
entry in :data:`VERSIONS`; an old one is never edited once a real contract has
signed it, and a test pins each hash so an edit cannot slip in unnoticed. Until
the first real contract, v1 is a draft and is edited in place.

The text was reviewed by Internet and agent consensus, not by a lawyer.
"""

from __future__ import annotations

import hashlib

__all__ = ["CURRENT", "VERSIONS", "terms_hash", "text"]

_V0 = """\
MakerMarket Standard Terms, version v0

1. Parties and roles
1.1 "Client" posts a job and pays for it.
1.2 "Worker" claims the job and delivers it.
1.3 "Arbiter" is an independent third party named on the Cover Page, if any.
1.4 "Platform" is the MakerMarket software and its operator.
1.5 The Platform is not a party to any job contract.

2. How a job contract forms
2.1 The Client posts a job with a draft Cover Page and a specification ("Spec").
2.2 A Worker claims the job.
2.3 The contract forms when the Client and Worker have both signed the same Cover Page with their Nostr keys.
2.4 Each signature covers the hash of the Cover Page, the hash of the Spec, and the hash of these Standard Terms.
2.5 The version of the terms in force for a job is the one whose hash was signed, and it never changes for that job.
2.6 The contract consists of the Cover Page, the Spec and these Standard Terms.
2.7 If they conflict, the Cover Page wins over the Spec, and the Spec wins over these Standard Terms.
2.8 A Cover Page field left blank means "none" or "not applicable".

3. Scope
3.1 The Worker will deliver the deliverables listed on the Cover Page, as described in the Spec.
3.2 Work outside the Spec is not part of the job unless both parties sign a new Cover Page.
3.3 The Client will supply the inputs and answers the Spec says it will supply.

4. Delivery and grading
4.1 The Worker delivers by the delivery date on the Cover Page, by the method the Spec names.
4.2 The Client grades the delivery against the rubric on the Cover Page.
4.3 Each grade pays the amount the rubric sets for it, in sats.
4.4 The Client must grade within the deemed-acceptance period on the Cover Page.
4.5 If the Client does not grade in time, the delivery is treated as graded at the deemed grade on the Cover Page.
4.6 The Worker may appeal a grade once, within 7 days of the grade.
4.7 If an Arbiter is named, the Arbiter decides the appeal.
4.8 If no Arbiter is named, the tie-breaker the Client named decides the appeal, and that grade settles the payment for that job.
4.9 A grade settles payment only, and neither party gives up any other legal right by accepting it.

5. Payment
5.1 All amounts are in bitcoin, stated in sats. A price shown in any other currency is only an estimate.
5.2 When the Worker's claim is accepted, the Client pays the deposit to the Worker over Lightning.
5.3 The deposit is the percentage of the top-grade price that the Cover Page sets.
5.4 Each milestone on the Cover Page falls due on its date, for the percentage of the top-grade price it sets, and the Client pays it to the Worker over Lightning.
5.5 The deposit and the milestones together are at most 90 percent of the top-grade price.
5.6 The deposit and paid milestones count toward the amount the rubric awards.
5.7 The rest of the award is the "Balance". If the deposit and paid milestones exceed the award, the Worker keeps them and the Balance is zero.
5.8 Each party pays its own network and routing fees.
5.9 The Platform never receives, holds or controls the deposit, a milestone, the Balance or any escrowed funds.

6. Escrow
6.1 Escrow is not offered under this version. Every job is a Lightning-only job under section 7.
6.2 A later version may offer escrow in a 2-of-3 Taproot output held by the Client, the Worker and an Arbiter.
6.3 If it does, the refund time-lock date that lets the Client alone recover the escrow will always fall before the deemed-acceptance time-lock date that lets the Worker alone claim it.

7. Lightning-only jobs
7.1 After grading, the Worker issues a Lightning invoice for the Balance.
7.2 The Client pays it within 7 days.
7.3 An unpaid Balance is a debt the Client owes the Worker.

8. Arbiter
8.1 Both parties choose the Arbiter together for each job, if they want one.
8.2 The Arbiter's fee is the one in the Arbiter's advertisement named on the Cover Page.
8.3 The Cover Page says who pays the Arbiter's fee.
8.4 The Arbiter decides appeals.
8.5 The Arbiter does not act for either party, and does not sign these terms as a party.

9. Ownership and licence
9.1 When the Client has paid the award in full, the Worker assigns the deliverables to the Client, unless the Cover Page names a different IP option.
9.2 Until then, the Client may use the deliverables only to evaluate them.
9.3 The Worker keeps its pre-existing tools, code, methods and know-how.
9.4 The Client gets a perpetual, non-exclusive licence to any pre-existing material built into a deliverable, for use with that deliverable.
9.5 Drafts and options the Client did not take stay with the Worker.
9.6 The Worker may show the work in its portfolio, unless confidentiality is on.
9.7 Third-party material is allowed only if the Cover Page or the Spec permits it, and its own licence applies.

10. Confidentiality
10.1 If confidentiality is on, each party keeps the other's non-public information secret and uses it only for the job.
10.2 This duty lasts 3 years after delivery, or as long as the information remains a trade secret.
10.3 It does not cover information that is public, already known, independently developed, or required by law to be disclosed.

11. Warranties
11.1 Each party has the right to enter this contract.
11.2 The Worker's deliverables are its original work, apart from material the Spec permits.
11.3 To the Worker's knowledge, the deliverables do not infringe anyone's rights.
11.4 No other warranty is given, express or implied.

12. Limitation of liability
12.1 Each party's total liability under a job is capped at the job's top-grade price.
12.2 Neither party is liable for lost profits or indirect damages.
12.3 The cap does not apply to fraud, wilful misconduct, a breach of confidentiality, or an unpaid Balance.

13. Independent contractor and taxes
13.1 The Worker is an independent contractor and controls how, when and where it works.
13.2 Nothing in this contract creates employment, partnership or agency.
13.3 Each party is responsible for its own taxes, reporting and licences.

14. Cancellation
14.1 Before a claim, the Client may withdraw a posting at any time, at no cost.
14.2 After the claim and before delivery, the Client may release the Worker, and the Worker keeps the deposit and any paid milestones.
14.3 If the Worker gives the job back before delivery, or misses the delivery date by more than 7 days, the Worker must return the deposit, and keeps any paid milestones.
14.4 After the Worker is released, the Client may post the job again, on changed terms, to another Worker.
14.5 After delivery, the job ends only through grading.

15. Disputes
15.1 The parties will try in good faith to settle any dispute directly first.
15.2 Disputes beyond the grade are governed by the law and heard in the venue on the Cover Page.
15.3 If the Cover Page leaves these blank, the law and courts of the Worker's place of residence apply.

16. Changes to these terms
16.1 New versions of the Standard Terms are published with a new version number and hash.
16.2 A new version applies only to jobs posted under it.

17. The Platform's role
17.1 The Platform publishes postings, records signatures and moves, and fetches invoices.
17.2 It grades only at the deemed grade under section 4.5, and it does not choose Arbiters, hold funds or guarantee payment.
17.3 It is provided as is.
17.4 To the extent the law allows, the Platform is not liable for any job.

18. General
18.1 These terms, the Cover Page and the Spec are the whole agreement for the job.
18.2 If one clause cannot be enforced, the rest still stands.
18.3 Neither party may transfer the job without the other's signed consent.
18.4 Notices are signed Nostr messages between the parties' npubs.
"""

_V1 = """\
Task Booth Standard Terms, version v1

1. Parties and roles
1.1 "Client" posts a job and pays for it.
1.2 "Worker" claims the job and delivers it.
1.3 "Arbiter" is an independent third party named on the Cover Page, if any.
1.4 "Platform" is the Task Booth software and its operator, and any site built on it.
1.5 The Platform is not a party to any job contract.
1.6 "Mint" is the Cashu mint named on the Cover Page of a job held in escrow.
1.7 "Inspector" is an independent third party named on the Cover Page, if any, who gives an opinion of the finished work.

2. How a job contract forms
2.1 The Client posts a job with a draft Cover Page and a specification ("Spec").
2.2 A Worker claims the job.
2.3 The contract forms when the Client and Worker have both signed the same Cover Page with their Nostr keys.
2.4 If an Arbiter is named, the Arbiter signs the same Cover Page before the delivery, to accept the role.
2.5 Each signature covers the hash of the Cover Page, the hash of the Spec, and the hash of these Standard Terms.
2.6 The version of the terms in force for a job is the one whose hash was signed, and it never changes for that job.
2.7 The contract consists of the Cover Page, the Spec and these Standard Terms.
2.8 If they conflict, the Cover Page wins over the Spec, and the Spec wins over these Standard Terms.
2.9 A Cover Page field left blank means "none" or "not applicable".

3. Scope
3.1 The Worker will deliver the deliverables listed on the Cover Page, as described in the Spec.
3.2 Work outside the Spec is not part of the job unless both parties sign a new Cover Page.
3.3 The Client will supply the inputs and answers the Spec says it will supply.

4. Delivery and grading
4.1 The Worker delivers by the delivery date on the Cover Page, by the method the Spec names.
4.2 The Client grades the delivery against the rubric on the Cover Page.
4.3 Each grade pays the amount the rubric sets for it, in sats.
4.4 The Client must grade within the deemed-acceptance period on the Cover Page.
4.5 If the Client does not grade in time, the delivery is treated as graded at the deemed grade on the Cover Page.
4.6 The Worker may appeal a grade once, within 7 days of the grade.
4.7 If an Arbiter is named, the Arbiter decides the appeal.
4.8 If no Arbiter is named, the tie-breaker the Client named decides the appeal, and that grade settles the payment for that job.
4.9 A grade settles payment only, and neither party gives up any other legal right by accepting it.
4.10 The Cover Page may set late steps. Once a delivery is later than a step's hours past the delivery date, that step's percentage comes off the amount the grade awards, and the last such step applies.

5. Payment
5.1 All amounts are in bitcoin, stated in sats. A price shown in any other currency is only an estimate.
5.2 When the Worker's claim is accepted, the Client pays the deposit to the Worker over Lightning.
5.3 The deposit is the percentage of the top-grade price that the Cover Page sets.
5.4 Each milestone on the Cover Page falls due on its date, for the percentage of the top-grade price it sets, and the Client pays it to the Worker over Lightning.
5.5 The deposit and the milestones together are at most 90 percent of the top-grade price.
5.6 The deposit and paid milestones count toward the amount the rubric awards.
5.7 The rest of the award is the "Balance". If the deposit and paid milestones exceed the award, the Worker keeps them and the Balance is zero.
5.8 Each party pays its own network and routing fees.
5.9 The Platform never receives, holds or controls the deposit, a milestone, the Balance or any escrowed funds.
5.10 Escrowed funds are held by the Mint, under section 6.

6. Escrow
6.1 A job is held in escrow only if its Cover Page names an Arbiter and asks for escrow. Every other job is a Lightning-only job under section 7.
6.2 The "Escrowed Amount" is the top-grade price less the deposit and the milestones.
6.3 After the claim, and once the Client, the Worker and the Arbiter have signed, the Client pays the Escrowed Amount to the Mint by a Lightning invoice.
6.4 The Mint holds it as ecash locked to the Nostr keys of the Client, the Worker and the Arbiter.
6.5 Any two of the three may release the escrow, in shares between the Worker and the Client.
6.6 From the refund date on the Cover Page, the Client alone may take back the whole escrow.
6.7 The refund date is the number of days on the Cover Page after the delivery date.
6.8 The Worker may not mark the delivery done until the escrow is funded.
6.9 After grading, the Worker's share is the Balance under section 5.7, and the Client's share is the rest.
6.10 The Client and the Worker sign that split together, or the Arbiter signs it with either of them.
6.11 The Arbiter signs with the Worker when the Client did not grade in time (section 4.5), signs with the Client after a failure to deliver without cause (section 14.10), and signs as the Arbiter decided after an appeal or a dispute.
6.12 Each share is paid to its owner's Lightning address.
6.13 The Mint's fees to move the escrow and any Lightning routing fees come out of the escrow before it is shared.
6.14 The Mint is chosen from those the Platform offers, and the Cover Page names it.
6.15 The Mint cannot move the escrowed sats without the signatures in sections 6.5 and 6.6.
6.16 If the Mint fails, stops or will not pay before the escrow is released, the escrowed sats may be lost, and the Balance is then owed under section 7.
6.17 The Platform builds and submits the escrow's transactions, and holds no key that can move them.

7. Lightning-only jobs
7.1 After grading, the Worker issues a Lightning invoice for the Balance.
7.2 The Client pays it within 7 days.
7.3 An unpaid Balance is a debt the Client owes the Worker.

8. Arbiter and Inspector
8.1 The Client names the Arbiter on the Cover Page, and the Worker accepts the Arbiter by claiming the job and signing.
8.2 Any fee the Arbiter charges is agreed with the Arbiter, and the Cover Page or that agreement says who pays it.
8.3 The Arbiter decides appeals, and disputes over a job held in escrow.
8.4 The Arbiter signs escrow releases only as the Arbiter decided, or as section 6.11 provides.
8.5 The Arbiter does not act for either party, and is not a party to the job contract.
8.6 The Client may name an Inspector on the Cover Page, and the Inspector signs the same Cover Page as the parties.
8.7 After delivery, the Inspector may give one opinion of the finished work, signed with the Inspector's Nostr key.
8.8 The opinion is advice: it does not grade the work, decide an appeal, or hold or release escrow.
8.9 The Arbiter may take the opinion into account when deciding an appeal or a claim of cause.
8.10 The Inspector does not act for either party, is not a party to the job contract, and is paid only as agreed with the Inspector.

9. Ownership and licence
9.1 When the Client has paid the award in full, the Worker assigns the deliverables to the Client, unless the Cover Page names a different IP option.
9.2 Until then, the Client may use the deliverables only to evaluate them.
9.3 The Worker keeps its pre-existing tools, code, methods and know-how.
9.4 The Client gets a perpetual, non-exclusive licence to any pre-existing material built into a deliverable, for use with that deliverable.
9.5 Drafts and options the Client did not take stay with the Worker.
9.6 The Worker may show the work in its portfolio, unless confidentiality is on.
9.7 Third-party material is allowed only if the Cover Page or the Spec permits it, and its own licence applies.

10. Confidentiality
10.1 If confidentiality is on, each party keeps the other's non-public information secret and uses it only for the job.
10.2 This duty lasts 3 years after delivery, or as long as the information remains a trade secret.
10.3 It does not cover information that is public, already known, independently developed, or required by law to be disclosed.

11. Warranties
11.1 Each party has the right to enter this contract.
11.2 The Worker's deliverables are its original work, apart from material the Spec permits.
11.3 To the Worker's knowledge, the deliverables do not infringe anyone's rights.
11.4 No other warranty is given, express or implied.

12. Limitation of liability
12.1 Each party's total liability under a job is capped at the job's top-grade price.
12.2 Neither party is liable for lost profits or indirect damages.
12.3 The cap does not apply to fraud, wilful misconduct, a breach of confidentiality, or an unpaid Balance.

13. Independent contractor and taxes
13.1 The Worker is an independent contractor and controls how, when and where it works.
13.2 Nothing in this contract creates employment, partnership or agency.
13.3 Each party is responsible for its own taxes, reporting and licences.

14. Cancellation
14.1 Before a claim, the Client may withdraw a posting at any time, at no cost.
14.2 After the claim and before delivery, the Client may release the Worker, and the Worker keeps the deposit and any paid milestones.
14.3 If the Worker gives the job back before delivery, the Worker must return the deposit, and keeps any paid milestones.
14.4 After the Worker is released, the Client may post the job again, on changed terms, to another Worker.
14.5 After delivery, the job ends only through grading.
14.6 A job held in escrow that ends without grading is settled by a split signed under section 6.5, or by the Client taking back the escrow under section 6.6.
14.7 If the work is still not delivered when it is later than the failure point on the Cover Page, the job ends for failure to deliver.
14.8 Within 7 days of the failure, the Worker may claim cause once, with reasons, such as the Client not supplying what section 3.3 requires. The Arbiter decides the claim within 7 days. If no Arbiter is named, no claim is made in time, or the Arbiter rejects the claim, the failure is without cause.
14.9 After a failure without cause, payments already made stay where they are. The Platform does not move them back, and any claim to them is under section 15.
14.10 After a failure without cause, for a job held in escrow, the Arbiter signs with the Client to release the whole escrow to the Client.
14.11 After a failure without cause, every deliverable, draft and material the Worker handed to the Client before the end becomes the Client's property, despite sections 9.1, 9.2 and 9.5.
14.12 If the Arbiter upholds the claim of cause, the Arbiter decides how any escrow is shared, and section 9 applies as written.

15. Disputes
15.1 The parties will try in good faith to settle any dispute directly first.
15.2 Disputes beyond the grade are governed by the law and heard in the venue on the Cover Page.
15.3 If the Cover Page leaves these blank, the law and courts of the Worker's place of residence apply.

16. Changes to these terms
16.1 New versions of the Standard Terms are published with a new version number and hash.
16.2 A new version applies only to jobs posted under it.

17. The Platform's role
17.1 The Platform publishes postings, records signatures and moves, fetches invoices, and builds and submits escrow transactions.
17.2 It grades only at the deemed grade under section 4.5, and it does not choose Arbiters, hold funds or guarantee payment.
17.3 It offers Mints for escrow, and does not control them.
17.4 It is provided as is.
17.5 To the extent the law allows, the Platform is not liable for any job.

18. General
18.1 These terms, the Cover Page and the Spec are the whole agreement for the job.
18.2 If one clause cannot be enforced, the rest still stands.
18.3 Neither party may transfer the job without the other's signed consent.
18.4 Notices are signed Nostr messages between the parties' npubs.
"""

#: Every version ever published, by name. Never edit one; add the next.
VERSIONS: dict[str, str] = {"v0": _V0, "v1": _V1}

#: The version a newly posted job is put under.
CURRENT = "v1"


def text(version: str = "") -> str:
    """The full text of ``version`` (the current one when empty), or ``ValueError``."""
    name = version or CURRENT
    if name not in VERSIONS:
        raise ValueError(f"There is no version {name!r} of the Standard Terms. Versions: {', '.join(VERSIONS)}.")
    return VERSIONS[name]


def terms_hash(version: str = "") -> str:
    """The sha256 of ``version``'s UTF-8 text, in hex: what a signature covers."""
    return hashlib.sha256(text(version).encode("utf-8")).hexdigest()
