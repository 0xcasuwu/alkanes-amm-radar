#!/usr/bin/env python3
"""
Local (no-RPC) alkanes protostone decoder.
Replays alkanes-rs Runestone->Protostone->Cellpack parsing from a raw OP_RETURN,
so an investigator can decode alkanes calls / DIESEL mints during an RPC outage.

Ground truth (alkanes-rs @ /Users/erickdelgado/Documents/github/alkanes-rs):
 - Runes magic:  OP_RETURN(0x6a) OP_PUSHNUM_13(0x5d)   [runestone.rs:25]
 - Protostone carrier tag = 16383 (0x3FFF) "Protocol", take_all  [tag.rs:26, runestone.rs:132]
 - Repack: 15 bytes/u128 LE (drop TOP byte) then LEB128 again  [byte_utils.rs:27, protostone.rs:196,301]
 - Protostone stream: [protocol_tag, length, length*ints]* until protocol_tag==0  [protostone.rs:309]
 - Inner field tags: 81=Message(cellpack) 83=Burn 91=ProtoPointer 93=Refund 95=From 0=Body/edicts  [tag.rs]
 - alkanes protocol_tag == 1   [message.rs:197]
 - Cellpack: message bytes LEB-decoded -> [target.block, target.tx, *inputs].
     inputs[0]=opcode ONLY when inputs is non-empty; a bare [block,tx] cellpack has NO opcode.  [cellpack.rs:64]
 - DIESEL mint <=> target==(2,0) and inputs[0]==77 (opcode 77 = Mint, stable across all genesis variants) [genesis-alkane lib.rs:30]

CENOTAPH DISCIPLINE (critical for forensic fidelity — audit finding):
 alkanes-rs VOIDS a runestone (never extracts Tag 16383, so protostones are IGNORED on-chain) when:
   - any leftover UNRECOGNIZED EVEN tag remains  [runestone.rs:120 Flaw::UnrecognizedEvenTag]
   - a non-pushdata opcode (>=0x4f) appears after the magic  [runestone.rs:261 Flaw::Opcode]
   - a push is truncated / script invalid  [runestone.rs:264 Flaw::InvalidScript]
   - a varint is overlong/overflowing/unterminated  [varint.rs:11-33 Flaw::Varint]
 In every such case the indexer runs NOTHING. This decoder reports {"cenotaph":True,"flaw":...,"protostones":[]}
 so a voided tx is never mis-reported as carrying a live mint/attack.

AlkaneId block semantics [id.rs]: 1=CREATE deploy, 3=CREATERESERVED, 2/4/32=created/deployed,
 5/6=factory clone. So [2,0]=DIESEL(genesis), [4,65522]=AMM factory, [32,0]=frBTC.
"""
import sys, json

# Recognized Runes tags that are consumed before the even-tag cenotaph check.
# Mint(20) consumes TWO values; all others one. [runestone.rs decipher / tag.rs]
_RUNES_TAGS_ONE = {2, 4, 6, 8, 10, 12, 14, 16, 18, 22, 1, 3, 5}  # Flags..Symbol
_RUNES_TAG_MINT = 20          # consumes 2 values (block, tx)
_PROTOCOL_TAG = 16383         # 0x3FFF — the protostone carrier

class Cenotaph(Exception):
    def __init__(self, flaw): self.flaw = flaw

def _leb128(data, allow_partial=False):
    """LEB128/varint decode mirroring alkanes-rs varint::decode.
    Raises Cenotaph('varint') on overlong(>19 bytes), overflow, or unterminated tail."""
    out=[]; i=0; n=len(data)
    while i < n:
        r=0; k=0; done=False
        while i < n:
            b=data[i]; i+=1
            if k > 18:
                raise Cenotaph('varint')            # overlong (>19 bytes) [varint.rs:11]
            if k == 18 and (b & 0x7c) != 0:
                raise Cenotaph('varint')            # 19th byte overflow bits [varint.rs:22]
            r |= (b & 0x7f) << (7*k); k += 1
            if not (b & 0x80): done=True; break
        if not done:
            if allow_partial: break                 # tolerant mode for inner streams
            raise Cenotaph('varint')                # unterminated group [varint.rs:33]
        out.append(r)
    return out

def extract_runestone_payload(spk_hex):
    """Concatenate all pushdata after OP_RETURN OP_PUSHNUM_13.
    Returns bytes, or None if not a runestone, or raises Cenotaph on opcode/invalid-script."""
    b=bytes.fromhex(spk_hex)
    if len(b)<2 or b[0]!=0x6a: return None
    i=1
    if b[i]!=0x5d: return None                       # OP_PUSHNUM_13 magic
    i+=1
    payload=b''
    while i < len(b):
        op=b[i]; i+=1
        if op < 0x4c:
            ln=op
        elif op==0x4c:
            if i+1>len(b): raise Cenotaph('invalid-script')
            ln=b[i]; i+=1
        elif op==0x4d:
            if i+2>len(b): raise Cenotaph('invalid-script')
            ln=b[i]|(b[i+1]<<8); i+=2
        elif op==0x4e:
            if i+4>len(b): raise Cenotaph('invalid-script')
            ln=int.from_bytes(b[i:i+4],'little'); i+=4
        else:
            raise Cenotaph('opcode')                 # non-pushdata opcode >=0x4f voids the tx
        if i+ln > len(b): raise Cenotaph('invalid-script')   # truncated push
        payload += b[i:i+ln]; i+=ln
    return payload

def parse_runestone_fields(ints):
    """Pair (tag,value); Body(0) greedily takes the tail. Returns fields dict tag->[vals]."""
    fields={}; i=0; n=len(ints)
    while i < n:
        tag=ints[i]
        if tag==0:
            fields.setdefault(0,[]).extend(ints[i+1:]); break
        if i+1 >= n:
            raise Cenotaph('truncated-field')        # tag with no value [message.rs:44]
        fields.setdefault(tag,[]).append(ints[i+1]); i+=2
    return fields

def check_cenotaph(fields):
    """Model recognized-tag consumption; any leftover EVEN tag => cenotaph. [runestone.rs:120]"""
    leftover=set(fields.keys())
    for t in list(_RUNES_TAGS_ONE)+[_RUNES_TAG_MINT, _PROTOCOL_TAG, 0]:
        leftover.discard(t)
    for t in leftover:
        if t % 2 == 0:
            raise Cenotaph('unrecognized-even-tag')

def join_to_bytes(words):
    """15 bytes/u128 LE, drop TOP byte. [protostone.rs:196 / byte_utils.rs:27]"""
    out=b''
    for w in words:
        out += (w & ((1<<120)-1)).to_bytes(15,'little')
    return out

def parse_protostones(protocol_words):
    raw=join_to_bytes(protocol_words)
    stream=_leb128(raw, allow_partial=True)          # inner stream: trailing zero-pad is expected
    stones=[]; i=0
    while i < len(stream):
        ptag=stream[i]; i+=1
        if ptag==0: break                            # trailing zero padding terminates
        if i>=len(stream): break
        length=stream[i]; i+=1
        body=stream[i:i+length]; i+=length
        stones.append((ptag, body))
    return stones

def protostone_fields(body):
    """Pair body ints into tag->[vals]; key 0 greedily takes remaining (edicts)."""
    fields={}; i=0
    while i < len(body):
        key=body[i]; i+=1
        if key==0:
            fields.setdefault(0,[]).extend(body[i:]); break
        if i>=len(body): break
        fields.setdefault(key,[]).append(body[i]); i+=1
    return fields

def decode_edicts(raw):
    """Delta-decode Body(0) ints into absolute [block,tx,amount,output] edicts.
    [protostone.rs:6-19 next_protostone_edict_id, :61-82]"""
    edicts=[]; last=(0,0)
    if len(raw) % 4 != 0:
        return {"edicts_raw": raw, "error": "edict-set-size (not multiple of 4)"}
    for k in range(0, len(raw), 4):
        d_block, d_tx, amount, output = raw[k:k+4]
        block = last[0] + d_block
        tx = (last[1] + d_tx) if d_block == 0 else d_tx
        edicts.append({"id": [block, tx], "amount": amount, "output": output})
        last = (block, tx)
    return {"edicts": edicts}

def decode_cellpack(message_words):
    mb=join_to_bytes(message_words)
    ints=_leb128(mb, allow_partial=True)
    if len(ints)<2: return None
    # NOTE: trailing 0 inputs from 15-byte padding are REAL (alkanes-rs does not trim). Keep them.
    return {"target":{"block":ints[0],"tx":ints[1]}, "inputs":ints[2:]}

def decode_op_return(spk_hex):
    try:
        payload=extract_runestone_payload(spk_hex)
    except Cenotaph as c:
        return {"runestone":True, "cenotaph":True, "flaw":c.flaw, "protostones":[]}
    if payload is None: return {"runestone":False}
    try:
        ints=_leb128(payload)
        fields=parse_runestone_fields(ints)
        check_cenotaph(fields)
    except Cenotaph as c:
        return {"runestone":True, "cenotaph":True, "flaw":c.flaw, "protostones":[]}

    result={"runestone":True, "cenotaph":False,
            "pointer":fields.get(22,[None])[0], "runes_tags":sorted(fields.keys())}
    proto=fields.get(_PROTOCOL_TAG, [])
    result["protocol_word_count"]=len(proto)
    result["protostones"]=[]
    for ptag, body in parse_protostones(proto):
        pf=protostone_fields(body)
        entry={"protocol_tag":ptag,
               "pointer":pf.get(91,[None])[0], "refund":pf.get(93,[None])[0],
               "burn":pf.get(83,[None])[0], "from":pf.get(95,[None])[0]}
        msg=pf.get(81, [])
        if msg:
            cp=decode_cellpack(msg)
            entry["cellpack"]=cp
            if ptag==1 and cp:
                t=cp["target"]; ins=cp["inputs"]
                entry["alkane_call"]={"target":f'[{t["block"]},{t["tx"]}]',
                                      "opcode":ins[0] if ins else None, "args":ins[1:]}
                if t["block"]==2 and t["tx"]==0 and ins and ins[0]==77:
                    entry["DIESEL_MINT"]=True
        if 0 in pf:
            entry.update(decode_edicts(pf[0]))
        result["protostones"].append(entry)
    return result

if __name__=="__main__":
    spk=sys.argv[1] if len(sys.argv)>1 else sys.stdin.read().strip()
    print(json.dumps(decode_op_return(spk), indent=2, default=str))
