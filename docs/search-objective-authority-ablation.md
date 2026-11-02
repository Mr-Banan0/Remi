# Retrieval objective authority experiment

The original user query defines the retrieval task. Generated navigation queries can drift toward generic advice while retaining a constraint role. The experimental Jev instruction asks it to evaluate navigation suggestions against the original task and the role meaning, and to credit useful evidence outside those suggestions. It preserves the navigation text for cases where it is helpful. Production remains V84.

Six independent scenarios in English, Chinese and Spanish cover study scheduling, pet adoption, connector compatibility, travel documents and purchase budgets. Each is tested in both candidate orders.

objectives: 12/12, mean margin 0.2208
fallible_objectives: 12/12, mean margin 0.3492

Both variants pass these simple pairs. Increased score separation alone does not establish a Golden improvement. The replay CLI records the complete experimental question and its SHA256; paired replay freezes Planner outputs and clocks to isolate ranking.
