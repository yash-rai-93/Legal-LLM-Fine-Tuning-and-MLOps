import jsonlines, json
from openai import OpenAI
from pydantic import BaseModel, Field
from dotenv import load_dotenv
load_dotenv()

infilepath = 'dataset_engineering/raw/ContractNLI/contract_nli_long/contract_nli_long.jsonl'
outfilepath = 'dataset_engineering/processed/contractNLI_processesdHypotheses.jsonl'

class obligation(BaseModel):
    party: str = Field(description="Name of the party the obligation refers to")
    action: str = Field(description="The action within the obligation")
    condition: str | None = Field(description="The condition(s) of the action. This may be empty")

class obligation_list(BaseModel):
    obligations: list[obligation]

class risk(BaseModel):
    party: str = Field(description="Name of the party the obligation refers to")


client = OpenAI()
model = "gpt-4o"

def extract_obligations(data:list):
    system_message = """
        You are a legal analyst that extracts  from hypotheses on legal contracts. The following is an example of how you extract the fields: party, action, condition.

        Given:
        [
        {"hypothesis": "Receiving Party may share some Confidential Information with some third-parties (including consultants, agents and professional advisors)."},
        {"hypothesis": "Receiving Party shall notify Disclosing Party in case Receiving Party is required by law, regulation or judicial process to disclose any Confidential Information."},
        {"hypothesis": "Agreement shall not grant Receiving Party any right to Confidential Information."}
        ]

        You return:
        [
        {"party": "Receiving Party", "action": "may share Confidential Information", "condition": "only with third parties including consultants, agents, and professional advisors"},
        {"party": "Receiving Party", "action": "shall notify Disclosing Party", "condition": "if required by law, regulation, or judicial process to disclose Confidential Information"},
        {}
        ]
    """

    response = client.responses.parse(
        model=model,
        input=[
            {
                "role": "system", 
                "content": system_message},
            {
                "role": "user",
                "content": str(data),
            },
        ],
        text_format=obligation_list,
        temperature=0.4,                  # <--- Added for balanced creativity/logic
        #max_completion_tokens=1500,       # <--- Added to prevent JSON truncation
        top_p=1.0,                        # <--- Explicitly set for clarity
        frequency_penalty=0.0,            # <--- Ensure strict legal terminology
        presence_penalty=0.0              # <--- Ensure strict legal terminology
    )

    return response.output_parsed



with jsonlines.open(infilepath, mode='r') as reader, \
    jsonlines.open('output.jsonl', mode='w') as writer:
    for line in reader:
        try:
            hypothesises = line['hypothesises/labels']
            json_hypothesis = extract_obligations(hypothesises).obligations
            for h in hypothesises:
                print(h)
            print()
            for h in json_hypothesis:
                print(h)
            exit(0)
        except json.JSONDecodeError:
            print(f"Error decoding JSON from line: {line.strip()}")
