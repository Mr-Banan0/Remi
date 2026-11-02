# Retrieval objective authority experiment

The original user query defines the retrieval task. Generated navigation queries can drift toward generic advice while retaining a constraint role. The experimental Jev instruction asks it to evaluate navigation suggestions against the original task and the role meaning, and to credit useful evidence outside those suggestions. It preserves the navigation text for cases where it is helpful. Production remains V84.

Six independent scenarios in English, Chinese and Spanish cover study scheduling, pet adoption, connector compatibility, travel documents and purchase budgets. Each is tested in both candidate orders.

objectives: 12/12, mean margin 0.2208
fallible_objectives: 12/12, mean margin 0.3492

Both variants pass these simple pairs. Increased score separation alone does not establish a Golden improvement. The replay CLI records the complete experimental question and its SHA256; paired replay freezes Planner outputs and clocks to isolate ranking.

## Paired complete Search replay

Both runs use the V83 query-boundary bank and freeze all Planner outputs and clocks from `search-v84-live-v83-20261102-r2`. Control `search-v84-authority-control-v83-20261102-r1` scores R@10 92.7350%, MRR@10 85.3846%. Experimental `search-v84-authority-v83-paired-20261102-r1` scores R@10 88.8889%, MRR@10 83.3333%. Both have R@100 97.4359%, lifecycle 3/3 and zero request errors. Candidate IDs and base scores match in 16/16 queries.

The instruction is rejected for production. The action query's first gold rank is 10 in control and 11 with the instruction. Synthetic pair improvements did not transfer to the complete candidate set. Correcting query construction requires a separate experiment; current production remains V84.

## Small query-template check

A separate embedding-only test compared original requests, advice-oriented navigation, and a programmatic feasibility template on the same six independent multilingual scenarios. It used twelve competing statements. The template placed the needed condition at ranks 3, 1, 2, 1, 4, 10 (study, pet adoption, dock compatibility, travel, Chinese study, Spanish budget), versus original-query ranks 3, 2, 2, 2, 3, 10. The weak improvement and remaining failures do not justify production or full Golden evaluation. Output is `/private/tmp/remi-action-query-check.json`.

## Planner examples small test

Two generic input/output examples (pottery class planning and house-party invitations) were appended only inside an isolated process. Six independent English/Chinese/Spanish requests included distracting suggestion cards. The examples made outputs describe personal conditions and later changes more consistently, including laptop compatibility instead of market reviews. Actual embedding retrieval in the shared twelve-statement pool did not consistently improve: baseline needed-condition ranks were 3, 2, 1, 1, 3, 7; example-augmented ranks were 6, 1, 2, 2, 3, 5. This variant is not promoted.

Evidence: `/private/tmp/remi-planner-action-examples.json`, `/private/tmp/remi-planner-extra-examples.json`, `/private/tmp/remi-planner-query-retrieval.json`. These are small semantic-navigation tests, not estimates of Golden accuracy. The evaluation follows diverse input/output example guidance from https://developers.openai.com/api/docs/guides/prompt-engineering; effectiveness is determined by local measurements.
