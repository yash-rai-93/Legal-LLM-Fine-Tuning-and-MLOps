"""ContractNLI's fixed hypothesis IDs, separate from model dependencies.

Source: https://stanfordnlp.github.io/contract-nli/ (CC BY 4.0).
Keep hypothesis meaning identical across data, prompts and evaluation.
"""

HYPOTHESES = {
    "nda-1": "All Confidential Information shall be expressly identified by the Disclosing Party.",
    "nda-2": "Confidential Information shall only include technical information.",
    "nda-3": "Confidential Information may include verbally conveyed information.",
    "nda-4": "Receiving Party shall not use any Confidential Information for any purpose other than the purposes stated in Agreement.",
    "nda-5": "Receiving Party may share some Confidential Information with some of Receiving Party's employees.",
    "nda-7": "Receiving Party may share some Confidential Information with some third-parties (including consultants, agents and professional advisors).",
    "nda-8": "Receiving Party shall notify Disclosing Party in case Receiving Party is required by law, regulation or judicial process to disclose any Confidential Information.",
    "nda-10": "Receiving Party shall not disclose the fact that Agreement was agreed or negotiated.",
    "nda-11": "Receiving Party shall not reverse engineer any objects which embody Disclosing Party's Confidential Information.",
    "nda-12": "Receiving Party may independently develop information similar to Confidential Information.",
    "nda-13": "Receiving Party may acquire information similar to Confidential Information from a third party.",
    "nda-15": "Agreement shall not grant Receiving Party any right to Confidential Information.",
    "nda-16": "Receiving Party shall destroy or return some Confidential Information upon the termination of Agreement.",
    "nda-17": "Receiving Party may create a copy of some Confidential Information in some circumstances.",
    "nda-18": "Receiving Party shall not solicit some of Disclosing Party's representatives.",
    "nda-19": "Some obligations of Agreement may survive termination of Agreement.",
    "nda-20": "Receiving Party may retain some Confidential Information even after the return or destruction of Confidential Information.",
}

LABELS = frozenset({"entailment", "contradiction", "not_mentioned"})
SCHEMA_VERSION = "contractnli-cpu-v1"
