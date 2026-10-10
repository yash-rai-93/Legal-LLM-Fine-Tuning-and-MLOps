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