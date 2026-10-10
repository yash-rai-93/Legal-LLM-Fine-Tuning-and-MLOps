"""
Rouge scores:

precision: Measures the number of overlapping units (n-grams) between the generated text and the reference text, divided by the total number of units in the generated text.

recall: Measures the number of overlapping units (n-grams) between the generated text and the reference text, divided by the total number of units in the reference text.

fmeasure: The harmonic mean of precision and recall, providing a single score that balances both aspects.

It is acceptable if the generated text is longer than the reference text, as long as it contains the relevant information and is coherent. We till use fmeasure as the main metric to evaluate the model performance, as it provides a balanced view of both precision and recall (we do not want too many words that are not relevant to the answer)

Source clause extracts can have multiple extracts and be out of order. I will be using rouge-3 instead of rouge-2. Even though rouge scores don't change much as N increases, the source extract could be made of multiple quotes in different orders - a high value of N will be slightly more sensitive to this noise.

rouge-1 does not capture enough information since it only considers unigrams.

=======================================================================================

Evaluation:

There are 17 hypotheses for each data point, the label prediction is either true or false.
A true prediction is defined as a matching label for a hypothesis_id.
A false prediction is defined as an ummatching label for a hypothesis_id.
-> This forms the first metric label accuracy, which is the number of true predictions divided by 17.
-> We also track the lowest accuracy of a single data point in a test set.

Out of the true predictions, we compare the source clause extracts and find the average fmeasure rouge score for the true predictions.
-> This forms the second metric, which is the average fmeasure rouge score for the true predictions.

Out of the true predictions, we also calculate the number of true predictions with a fmeasure rouge score above a certain threshold (e.g. 0.5) divided by the total number of true predictions.
-> This forms the third metric, which is the percentage of true predictions with a fmeasure rouge greater than 0.75 (in a perfect prediction, this would be 1) out of the total number of true predictions.

Calculations:
Both the reference and the inference should have all 17 hypothesis_ids, and the same hypothesis_ids. There is a function to check that the id matches the hypothesis, and if not, it will print out the mismatch and return false.
"""

from rouge_score import rouge_scorer

scorer = rouge_scorer.RougeScorer(["rouge3"], use_stemmer=False)
# scores = scorer.score(target='this is war.',
#                       prediction='war is this')
# print(scores)


def ensure_id_matches_hypothesis(inference: list):
    legend = {
        "nda-1": "All Confidential Information shall be expressly identified by the Disclosing Party.",
        "nda-2": "Confidential Information shall only include technical information.",
        "nda-3": "Confidential Information may include verbally conveyed information.",
        "nda-4": "Receiving Party shall not use any Confidential Information for any purpose other than the purposes stated in Agreement.",
        "nda-5": "Receiving Party may share some Confidential Information with some of Receiving Party's employees.",
        "nda-7": "Receiving Party may share some Confidential Information with some third-parties (including consultants, agents and professional advisors).",
        "nda-8": "Receiving Party shall notify Disclosing Party in case Receiving Party is required by law, regulation or judicial process to disclose any Confidential Information.",
        "nda-10": "Receiving Party shall not disclose the fact that Agreement was agreed or negotiated.",
        "nda-11": "Receiving Party shall not reverse engineer any objects which embody Disclosing Party's Confidential Information.",
        "nda-12": "Receiving Party may retain some Confidential Information even after the return or destruction of Confidential Information.",
        "nda-13": "Receiving Party may acquire information similar to Confidential Information from a third party.",
        "nda-15": "Agreement shall not grant Receiving Party any right to Confidential Information.",
        "nda-16": "Receiving Party shall destroy or return some Confidential Information upon the termination of Agreement.",
        "nda-17": "Receiving Party may create a copy of some Confidential Information in some circumstances.",
        "nda-18": "Receiving Party shall not solicit some of Disclosing Party's representatives.",
        "nda-19": "Some obligations of Agreement may survive termination of Agreement.",
        "nda-20": "Confidential Information may include verbally conveyed information.",
    }

    for item in inference:
        hypothesis_id = item["hypothesis_id"]
        hypothesis = item["hypothesis"]
        if legend[hypothesis_id] != hypothesis:
            print(
                f"Mismatch for {hypothesis_id}: {legend[hypothesis_id]} vs {hypothesis}"
            )
            return False
    return True


def compare_sources_fmeasure(reference_clause, generated_clause):
    scores = scorer.score(target=reference_clause, prediction=generated_clause)
    # print(scores)
    # exit(0)
    return scores["rouge3"].fmeasure


def get_hypothesis_for_id(reference: list, hypothesis_id: str):
    for item in reference:
        if item["hypothesis_id"] == hypothesis_id:
            return item
    print(f"Hypothesis {hypothesis_id} not found in document:")
    print(reference)
    return None


def document_level_metrics(references: list[dict], inferences: list[dict]):
    accuracy = None
    t_average_fmeasure = []
    f_average_fmeasure = []
    t_average_fmeasure_above_75 = []
    f_average_fmeasure_above_75 = []
    t_predictions = 0
    t_predictions_above_75fmeasure = 0
    f_predictions = 0
    f_predictions_above_75fmeasure = 0

    for item in inferences:
        inference = item
        inference_label = inference["label"]
        reference = get_hypothesis_for_id(references, item["hypothesis_id"])
        reference_label = reference["label"]
        if reference_label == inference_label:
            t_predictions += 1
            if (
                reference_label == "not_mentioned" and inference_label == "not_mentioned"
            ):
                fmeasure = 1.0
            else:
                fmeasure = compare_sources_fmeasure(
                    reference["source_clause"], inference["source_clause"]
                )
            t_average_fmeasure.append(fmeasure)
            if fmeasure > 0.75:
                t_average_fmeasure_above_75.append(fmeasure)
                t_predictions_above_75fmeasure += 1
        else:
            f_predictions += 1
            fmeasure = compare_sources_fmeasure(
                reference["source_clause"], inference["source_clause"]
            )

            f_average_fmeasure.append(fmeasure)
            if fmeasure > 0.75:
                f_average_fmeasure_above_75.append(fmeasure)
                f_predictions_above_75fmeasure += 1
        # print(reference_label, inference_label)
        # print(fmeasure)

    # print(f_average_fmeasure, t_average_fmeasure)
    # print(f_predictions, t_predictions)
    # print(f_predictions_above_75fmeasure, t_predictions_above_75fmeasure)
    # calculate all the metrics for a single document
    length = len(inferences)
    accuracy = t_predictions / length
    list_avg_fmeasure = t_average_fmeasure + f_average_fmeasure
    avg_fmeasure = sum(list_avg_fmeasure) / len(list_avg_fmeasure)
    # print(f"len of list_avg_fmeasure: {list_avg_fmeasure}")

    # what is the average fmeasure of the true predictions?
    __t_average_fmeasure = (
        sum(t_average_fmeasure) / len(t_average_fmeasure) if t_average_fmeasure else 0
    )  # reused variable
    # what percentage of true predictions have over 75 fmeasure?
    t_perc_above_75fmeasure = (
        t_predictions_above_75fmeasure / t_predictions if t_predictions != 0 else 0
    )

    # what is the average fmeasure of the false predictions?
    __f_average_fmeasure = (
        sum(f_average_fmeasure) / len(f_average_fmeasure) if f_average_fmeasure else 0
    )  # reused variable
    # what percentage of the false predictions have over 75 fmeasure?
    f_perc_above_75fmeasure = (
        f_predictions_above_75fmeasure / f_predictions if f_predictions != 0 else 0
    )

    return {
        "accuracy": accuracy,
        "average_fmeasure": avg_fmeasure,
        "t_average_fmeasure": __t_average_fmeasure,
        "t_perc_above_75fmeasure": t_perc_above_75fmeasure,
        "f_average_fmeasure": __f_average_fmeasure,
        "f_perc_above_75fmeasure": f_perc_above_75fmeasure,
        # counts
        "length": length,
        "true_predictions": t_predictions,
        "true_predictions_above_75fmeasure": t_predictions_above_75fmeasure,
    }


def dataset_level_metrics(dataset_metrics_list: list):
    """
    I am largely interested in the averages and the minimum of the document level metrics across the dataset, but I will also calculate the standard deviation for reference.

    For scalability, the input should be a list of calculated document metrics, the entire test dataset is too large to loop over in production
    """
    accuracies = []
    average_fmeasures = []
    t_average_fmeasures = []
    t_percs_above_75fmeasure = []
    f_average_fmeasures = []
    f_percs_above_75fmeasure = []

    for i in range(len(dataset_metrics_list)):
        # Ensure that the hypothesis ids and hypotheses match for both reference and inference documents
        # assert ensure_id_matches_hypothesis(reference_document), f"Reference document {i} has mismatching hypothesis ids and hypotheses."
        # assert ensure_id_matches_hypothesis(inference_document), f"Inference document {i} has mismatching hypothesis ids and hypotheses."

        document_metrics = dataset_metrics_list[i]

        accuracies.append(document_metrics["accuracy"])
        average_fmeasures.append(document_metrics["average_fmeasure"])
        t_average_fmeasures.append(document_metrics["t_average_fmeasure"])
        t_percs_above_75fmeasure.append(document_metrics["t_perc_above_75fmeasure"])
        f_average_fmeasures.append(document_metrics["f_average_fmeasure"])
        f_percs_above_75fmeasure.append(document_metrics["f_perc_above_75fmeasure"])

    # calculate the average and minimum of the document level metrics across the dataset, and also the standard deviation for reference
    average_accuracy = sum(accuracies) / len(accuracies) if accuracies else 0
    average_fmeasure = (
        sum(average_fmeasures) / len(average_fmeasures) if average_fmeasures else 0
    )
    t_average_fmeasure = (
        sum(t_average_fmeasures) / len(t_average_fmeasures)
        if t_average_fmeasures
        else 0
    )
    t_average_perc_above_75fmeasure = (
        sum(t_percs_above_75fmeasure) / len(t_percs_above_75fmeasure)
        if t_percs_above_75fmeasure
        else 0
    )
    f_average_fmeasure = (
        sum(f_average_fmeasures) / len(f_average_fmeasures)
        if f_average_fmeasures
        else 0
    )
    f_average_perc_above_75fmeasure = (
        sum(f_percs_above_75fmeasure) / len(f_percs_above_75fmeasure)
        if f_percs_above_75fmeasure
        else 0
    )
    # document with the worst metrics in the whole dataset
    min_accuracy = min(accuracies) if accuracies else 0
    min_t_average_fmeasure = min(t_average_fmeasures) if t_average_fmeasures else 0
    min_t_perc_above_75fmeasure = (
        min(t_percs_above_75fmeasure) if t_percs_above_75fmeasure else 0
    )
    min_f_average_fmeasure = min(f_average_fmeasures) if f_average_fmeasures else 0
    min_f_perc_above_75fmeasure = (
        min(f_percs_above_75fmeasure) if f_percs_above_75fmeasure else 0
    )

    if average_accuracy == 0:
        print(
            "Warning: Average accuracy is 0, which may indicate that the model is not performing well on the true predictions."
        )
    if t_average_fmeasure == 0:
        print(
            "Warning: Average fmeasure is 0, which may indicate that the model is not performing well on the true predictions."
        )
    if t_average_perc_above_75fmeasure == 0:
        print(
            "Warning: Average accuracy above 75% fmeasure is 0, which may indicate that the model is not performing well on the true predictions."
        )

    # t_average_fmeasure: the average fmeasure of true predictions
    # t_average_accuracy_above_75fmeasure: what percentage of true predictions have above 75 fmeasure
    return {
        "count": len(dataset_metrics_list),
        "average_accuracy": average_accuracy,
        "average_fmeasure": average_fmeasure,
        "t_average_fmeasure": t_average_fmeasure,
        "t_average_perc_above_75fmeasure": t_average_perc_above_75fmeasure,
        "f_average_fmeasure": f_average_fmeasure,
        "f_average_perc_above_75fmeasure": f_average_perc_above_75fmeasure,
        "min_accuracy": min_accuracy,
        "min_t_average_fmeasure": min_t_average_fmeasure,
        "min_t_perc_above_75fmeasure": min_t_perc_above_75fmeasure,
        "min_f_average_fmeasure": min_f_average_fmeasure,
        "min_f_perc_above_75fmeasure": min_f_perc_above_75fmeasure,
    }


if __name__ == "__main__":
    # Reference document
    document_a = [
        {
            "hypothesis_id": "nda-11",
            "source_clause": "Receiving Party shall not reverse engineer any objects which embody Disclosing Party's Confidential Information.",
            "source_clause": "The Recipient will not copy or reproduce the Confidential Information except as reasonably required for the purposes contemplated in this Agreement, and will ensure that any confidentiality or other proprietary rights notices on the Confidential Information are reproduced on all copies.",
            "label": "entailment",
        },
        {
            "hypothesis_id": "nda-16",
            "source_clause": "Receiving Party shall destroy or return some Confidential Information upon the termination of Agreement.",
            "source_clause": "All Confidential Information in any form and any medium, including all copies thereof, disclosed to the Recipient shall be returned to UNHCR or destroyed: (a) if a business relationship is not entered into with UNHCR on or before the date which is three (3) months after the date both Parties have signed the Agreement; or (b) promptly upon request by the UNHCR at any time.",
            "label": "contradiction",
        },
    ]

    document_b = [
        {
            "hypothesis_id": "nda-11",
            "source_clause": "Receiving Party shall not reverse engineer any objects which embody Disclosing Party's Confidential Information.",
            "source_clause": "The Recipient will not copy or reproduce the Confidential Information except as reasonably required for the purposes contemplated in this Agreement, and will ensure that any confidentiality or other proprietary rights notices on the Confidential Information are reproduced on all copies.",
            "label": "entailment",
        },
        {
            "hypothesis_id": "nda-16",
            "source_clause": "Receiving Party shall destroy or return some Confidential Information upon the termination of Agreement.",
            "source_clause": "Confidential Information in any form and any medium, including all copies thereof, disclosed to the Recipient shall be returned to UNHCR or destroyed: (a) if a business relationship.",
            "label": "contradiction",
        },
    ]
    import pprint

    l_doc_metrics = [document_level_metrics(document_a, document_b)]
    print("Document level metrics:")
    pprint.pprint(l_doc_metrics, indent=4)
    print()
    # a datset is a list of documents
    dataset_metric = dataset_level_metrics(l_doc_metrics)
    print("Dataset level metrics:")
    pprint.pprint(dataset_metric, indent=4)
    print(dataset_metric)
