import glob
import json
import os

import requests

from Curve25519 import sign, verify
from address import proof_sender
from address import validate_address
from block_ops import load_block
from config import get_config
from config import get_timestamp_seconds
from data_ops import sort_list_dict
from hashing import create_nonce, blake2b_hash
from keys import load_keys
from logs import get_logger


def calculate_fee():
    return 1


def get_transaction(txid, logger):
    """return transaction based on txid"""
    transaction_path = f"transactions/{txid}.dat"
    if os.path.exists(transaction_path):
        with open(transaction_path, "r") as file:
            block_hash = json.load(file)
            block = load_block(block_hash=block_hash, logger=logger)

            for transaction in block["block_transactions"]:
                if transaction["txid"] == txid:
                    return transaction
    else:
        return None


def create_txid(transaction):
    return blake2b_hash(json.dumps(transaction))


def validate_uniqueness(transaction, logger):
    if get_transaction(transaction, logger=logger):
        return False
    else:
        return True


def incorporate_transaction(transaction, block_hash):
    reflect_transaction(transaction)
    index_transaction(transaction, block_hash=block_hash)


def validate_transaction(transaction, logger):
    assert isinstance(transaction, dict), "Data structure incomplete"
    assert validate_origin(transaction), "Invalid origin"
    assert validate_address(transaction["sender"]), f"Invalid sender {transaction['sender']}"
    assert validate_address(transaction["recipient"]), f"Invalid recipient {transaction['recipient']}"
    assert validate_uniqueness(transaction["txid"], logger=logger), f"Transaction {transaction['txid']} already exists"
    assert isinstance(transaction["fee"], int), "Transaction fee is not an integer"
    assert transaction["fee"] >= 0, "Transaction fee lower than zero"
    return True


def max_from_transaction_pool(transactions: list, key="fee") -> dict:
    """returns dictionary from a list of dictionaries with maximum value"""
    return max(sort_list_dict(transactions), key=lambda transaction: transaction[key])


def sort_transaction_pool(transactions: list, key="txid") -> list:
    """sorts list of dictionaries based on a dictionary value"""
    return sorted(
        sort_list_dict(transactions), key=lambda transaction: transaction[key]
    )


def get_account(address, create_on_error=True):
    """return all account information if account exists else create it"""
    account_path = f"accounts/{address}/balance.dat"
    if os.path.exists(account_path):
        with open(account_path, "r") as account_file:
            account = json.load(account_file)
        return account
    elif create_on_error:
        return create_account(address)
    else:
        return None


def reflect_transaction(transaction, revert=False):
    sender = transaction["sender"]
    recipient = transaction["recipient"]
    amount = transaction["amount"]

    if revert:
        change_balance(address=sender, amount=amount)
        change_balance(address=recipient, amount=-amount)
    else:
        change_balance(address=sender, amount=-amount)
        change_balance(address=recipient, amount=amount)


def change_balance(address: str, amount: int):
    while True:
        try:
            account_message = get_account(address)
            account_message["account_balance"] += amount
            assert (
                    account_message["account_balance"] >= 0
            ), "Cannot change balance into negative"

            with open(f"accounts/{address}/balance.dat", "w") as account_file:
                account_file.write(json.dumps(account_message))
        except Exception as e:
            raise ValueError(f"Failed setting balance for {address}: {e}")
        break
    return True


def unindex_transaction(transaction):
    tx_path = f"transactions/{transaction['txid']}.dat"
    sender_path = (
        f"accounts/{transaction['sender']}/transactions/{transaction['txid']}.lin"
    )
    recipient_path = (
        f"accounts/{transaction['recipient']}/transactions/{transaction['txid']}.lin"
    )
    while True:
        try:
            os.remove(tx_path)
            os.remove(sender_path)
            if sender_path != recipient_path:
                os.remove(recipient_path)
        except Exception as e:
            raise ValueError(
                f"Failed to unindex transaction {transaction['txid']}: {e}"
            )
        break


def get_transactions_of_account(account, logger):
    account_path = f"accounts/{account}/transactions"
    transaction_files = glob.glob(f"{account_path}/*.lin")
    tx_list = []

    for transaction in transaction_files:
        no_ext_no_path = os.path.basename(os.path.splitext(transaction)[0])
        tx_data = get_transaction(no_ext_no_path, logger=logger)
        tx_list.append(tx_data)

    return {"tx_list": tx_list}


def index_transaction(transaction, block_hash):
    tx_path = f"transactions/{transaction['txid']}.dat"
    with open(tx_path, "w") as tx_file:
        tx_file.write(json.dumps(block_hash))

    sender_path = f"accounts/{transaction['sender']}/transactions"
    if not os.path.exists(sender_path):
        os.makedirs(sender_path)
    with open(f"{sender_path}/{transaction['txid']}.lin", "w") as tx_file:
        tx_file.write("")

    recipient_path = f"accounts/{transaction['recipient']}/transactions"
    if not os.path.exists(recipient_path):
        os.makedirs(recipient_path)
    with open(f"{recipient_path}/{transaction['txid']}.lin", "w") as tx_file:
        tx_file.write("")


def create_account(address, balance=0):
    """create account if it does not exist"""
    account_path = f"accounts/{address}/balance.dat"
    if not os.path.exists(account_path):
        os.makedirs(f"accounts/{address}")

        account = {
            "account_balance": balance,
            "account_address": address,
        }

        with open(account_path, "w") as outfile:
            json.dump(account, outfile)
        return account
    else:
        return get_account(address)


def to_readable_amount(raw_amount: int) -> str:
    return f"{(raw_amount / 1000000000):.10f}"


def to_raw_amount(amount: [int, float]) -> int:
    return int(amount * 1000000000)


def check_balance(account, amount, fee):
    """for single transaction, check if the fee and the amount spend are allowable"""
    balance = get_account(account)["account_balance"]
    assert (
            balance - amount - fee > 0 <= amount
    ), f"{account} spending more than owned in a single transaction"
    return True


def get_senders(transaction_pool: list) -> list:
    sender_pool = []
    for transaction in transaction_pool:
        if transaction["sender"] not in sender_pool:
            sender_pool.append(transaction["sender"])
    return sender_pool


def validate_single_spending(transaction_pool: list, transaction):
    """validate spending of a single spender against his transactions in a transaction pool"""
    transaction_pool.append(transaction)  # future state

    sender = transaction["sender"]

    standing_balance = get_account(sender)["account_balance"]
    amount_sum = 0
    fee_sum = 0

    for pool_tx in transaction_pool:
        if pool_tx["sender"] == sender:
            check_balance(
                account=sender,
                amount=pool_tx["amount"],
                fee=pool_tx["fee"],
            )

            amount_sum += pool_tx["amount"]
            fee_sum += pool_tx["fee"]

            spending = amount_sum + fee_sum
            assert spending <= standing_balance, "Overspending attempt"
    return True


def validate_all_spending(transaction_pool: list):
    """validate spending of all spenders in a transaction pool against their transactions"""
    sender_pool = get_senders(transaction_pool)

    for sender in sender_pool:
        standing_balance = get_account(sender)["account_balance"]
        amount_sum = 0
        fee_sum = 0

        for pool_tx in transaction_pool:
            if pool_tx["sender"] == sender:
                check_balance(
                    account=sender,
                    amount=pool_tx["amount"],
                    fee=pool_tx["fee"],
                )

                amount_sum += pool_tx["amount"]
                fee_sum += pool_tx["fee"]

                spending = amount_sum + fee_sum
                assert spending <= standing_balance, "Overspending attempt"
    return True


def validate_origin(transaction: dict):
    """save signature and then remove it as it is not a part of the signed message"""

    transaction = transaction.copy()
    signature = transaction["signature"]
    del transaction["signature"]

    assert proof_sender(
        sender=transaction["sender"], public_key=transaction["public_key"]
    ), "Invalid sender"

    assert verify(
        signed=signature,
        message=json.dumps(transaction),
        public_key=transaction["public_key"],
    ), "Invalid sender"

    return True


def create_transaction(sender, recipient, amount, public_key, private_key, timestamp, data, fee):
    """construct transaction, then add txid, then add signature as last"""
    transaction_message = {
        "sender": sender,
        "recipient": recipient,
        "amount": amount,
        "timestamp": timestamp,
        "data": data,
        "nonce": create_nonce(),
        "fee": fee,
        "public_key": public_key,
    }
    txid = create_txid(transaction_message)
    transaction_message.update(txid=txid)

    signature = sign(private_key=private_key, message=json.dumps(transaction_message))
    transaction_message.update(signature=signature)

    return transaction_message


if __name__ == "__main__":
    logger = get_logger(file="transactions.log")

    print(
        get_transaction(
            "210777f644d43ff694f3d1b2f6412114bd53bf2db726388a1001440d214ff499",
            logger=logger,
        )
    )
    # print(get_account("noob23"))

    key_dict = load_keys()
    address = key_dict["address"]
    recipient = "ndo6a7a7a6d26040d8d53ce66343a47347c9b79e814c66e29"
    private_key = key_dict["private_key"]
    public_key = key_dict["public_key"]
    amount = to_raw_amount(0.1)
    data = {"data_id": "seek_id", "data_content": "some_actual_content"}

    config = get_config()
    ip = config["ip"]
    port = config["port"]

    for x in range(0, 50000):
        transaction = create_transaction(
            sender=address,
            recipient=recipient,
            amount=amount,
            data=data,
            public_key=public_key,
            timestamp=get_timestamp_seconds(),
            fee=calculate_fee(),
            private_key=private_key
        )

        print(transaction)
        print(validate_transaction(transaction, logger=logger))

        requests.get(f"http://{ip}:{port}/submit_transaction?data={json.dumps(transaction)}", timeout=30)

    tx_pool = json.loads(requests.get(f"http://{ip}:{port}/transaction_pool").text, timeout=30)