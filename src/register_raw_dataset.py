import pandas as pd
import uuid


def process_raw(
    context,
    input_uri,
    artifact_key,
    label_column,
    version,
    output_uri_path,
):
    """
    When you pass your S3 path via the inputs={"source_url": source_url} dictionary in your .run() command, MLRun intercepts that string and automatically converts it into a powerful mlrun.DataItem object before handing it to your prep_data function.
    """
    # print(type(context)) # <class 'mlrun.execution.MLClientCtx'>

    # permitted formats csv|parquet|pq|tsdb|kv
    # https://docs.mlrun.org/en/latest/api/mlrun.execution/index.html#mlrun.execution.MLClientCtx.log_dataset

    # input_uri is an s3 identifier to a file. Returns pandas dataframe
    df = input_uri.as_df()  # mlrun.get_dataitem(input_uri).as_df()

    # rename not mentioned to not_mentioned for better parsing later on
    df["label"] = df["label"].replace({"notmentioned": "not_mentioned"})

    # full document
    full_document = df.groupby(["document_id"]).nth(0)[["document_id", "text"]]

    # hypotheses
    def hypo_inferred(row):
        """
        Helper function
        """
        # Turn evidence into a single string separated by newlines
        evidence_list = row["evidence_texts"].tolist()
        evidence_list_str = ". ".join(evidence_list)

        hypothesis = row["hypothesis"]
        hypo_label = row["label"]
        hypo_id = row["hypothesis_id"]

        assert None not in [
            hypo_id,
            hypothesis,
            evidence_list_str,
            hypo_label,
        ], f"One of the values in {[hypo_id, hypothesis, evidence_list_str, hypo_label]} is None"

        assert all(
            isinstance(x, str)
            for x in [hypo_id, hypothesis, evidence_list_str, hypo_label]
        ), f"One of the values in {[hypo_id, hypothesis, evidence_list_str, hypo_label]} is not a string"

        data_dict = {
            "hypothesis_id": hypo_id,
            "label": hypo_label,
            "hypothesis": hypothesis,
            "source_clause": evidence_list_str,
        }
        # return the dictionary as a string
        return data_dict

    hypotheses_inferred = df.copy()[
        ["document_id", "evidence_texts", "hypothesis", "label", "hypothesis_id"]
    ]
    hypotheses_inferred["inference"] = hypotheses_inferred.apply(hypo_inferred, axis=1)
    hypotheses_inferred = hypotheses_inferred[["document_id", "inference"]]

    # aggregate the hypo_infer strings into a list by document_id
    hypotheses_inferred_byid = (
        hypotheses_inferred.groupby("document_id")["inference"].agg(list).reset_index()
    )

    # merge full document, and processed hypotheses columns
    # columns are now: document_id, text, inference (str of long dictionary)
    final_df = pd.merge(
        full_document, hypotheses_inferred_byid, on="document_id", how="inner"
    )

    # Add additional columns
    final_df["model_repo"] = ""
    final_df["model_repo_version"] = ""
    final_df["timestamp"] = pd.Timestamp.now()
    final_df["origin"] = "in_house"

    # Add unique identifier column
    uuids = [str(uuid.uuid4()) for _ in range(len(final_df))]
    final_df.insert(0, "id", uuids)

    # jsonl_string = final_df.to_json(orient="records", lines=True)
    # write to new location on S3 as an artifact in jsonl
    # context.log_artifact(item=artifact_key,
    #                      body=jsonl_string,
    #                      tag=version,
    #                      format='jsonl',
    #                      artifact_path=f'{output_uri_path}/{version}', # output_uri_path: s3://legal-llama-data/registered
    #                      upload=True)

    # write to new location on S3
    context.log_dataset(
        key=artifact_key,
        tag=version,
        df=final_df,
        index="id",
        label_column=label_column,
        artifact_path=f"{output_uri_path}/{version}",  # output_uri_path: s3://legal-llama-data/registered
        format="parquet",
        # labels={'version':version}, # don't use this, messes up the artifact path
        upload=True,
    )

    print(f"{input_uri} processed and written to {output_uri_path}/{version}")
