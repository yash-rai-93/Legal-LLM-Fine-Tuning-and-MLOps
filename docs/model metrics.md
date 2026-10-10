# Model and Monitoring Metrics

[Back to the README](../README.md) | [Recorded experiments](training-and-evaluation.md)

This describes [utils_evaluate_model.py](../src/utils_evaluate_model.py) and the [Lambda gateway](../images/lambda_gateway/model_gateway.py).

## Offline evaluation

Each contract has 17 expected hypotheses and labels `entailment`, `contradiction`, or `not_mentioned`. Labels are matched to references by hypothesis ID.

| Metric | Implementation |
| --- | --- |
| Document accuracy | Matching labels divided by generated hypothesis count |
| Document `average_fmeasure` | Mean extraction score across generated hypotheses |
| Dataset `average_accuracy` | Mean of document accuracies |
| Dataset `average_fmeasure` | Mean of document extraction averages |
| `t_average_fmeasure` | Mean of document extraction averages for matching labels |
| `f_average_fmeasure` | Mean of document extraction averages for non-matching labels |
| `t_average_perc_above_75fmeasure` | Mean of document fractions of matching-label predictions scoring strictly above 0.75 |
| `f_average_perc_above_75fmeasure` | Equivalent fraction for non-matching labels |
| `min_*` | Minimum corresponding document metric |
| `count` | Number of scored documents |

Accuracy divides by generated hypothesis count, not a hard-coded 17. Verify hypothesis completeness and uniqueness before interpreting it.

`t_`/`f_` denote label matches/mismatches, not NLI classes. A document with no predictions in one group receives zero for that group's average metrics; dataset averaging includes that zero.

### Extraction scores

ROUGE-3 is calculated with stemming disabled:

- **Precision:** overlapping trigram count relative to generated trigram count.
- **Recall:** overlapping trigram count relative to reference trigram count.
- **F-measure:** harmonic mean of precision and recall.

Usually, the reference `source_clause` is compared with the generated `source_clause`. Correct `not_mentioned` predictions receive a special extraction score of **1.0**.

The resulting aggregate includes no-evidence cases. It is neither classification F1 nor an evidence-only ROUGE average.

### Output filtering

vLLM evaluation scripts parse JSON and extract `Hypotheses`. Malformed JSON or empty hypothesis lists are skipped before scoring. Report attempted/scored counts and excluded-response counts alongside the metrics.

## Production monitoring

The gateway compares non-`not_mentioned` evidence with the entire input contract using ROUGE-3 precision. It emits their average as `CARP_3`; when no clauses qualify, the current code emits `1.0`.

Label counts are emitted by hypothesis ID. Long-term metrics include endpoint/adapter dimensions; separate short-term metrics support rollout monitoring.

ROUGE measures tokenized n-gram overlap. It does not guarantee a verbatim, contiguous source span or establish semantic correctness. The [ROUGE implementation](https://github.com/google-research/google-research/blob/master/rouge/rouge_scorer.py) documents the overlap calculation.

Exact span validation, per-class quality reporting, and JSON-validity metrics are proposed additions, not components of the historical aggregate.

## Comparing runs

Align document IDs, prompt versions, inference settings, model/adapter revisions, metric code, and exclusions. The original 119-document baseline and 121-document example have incomplete provenance; their difference should not be presented as a controlled fine-tuning gain.
