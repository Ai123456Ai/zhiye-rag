"""Explicit document version policy; never infer authority from names or scores."""
import re
from collections import defaultdict
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class DocumentMetadata(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')

    document_key: str = Field(default='', max_length=100)
    version: str = Field(default='', max_length=80)
    effective_from: date | None = None
    status: Literal['active', 'archived'] = 'active'
    scope: str = Field(default='', max_length=100)

    @field_validator('effective_from', mode='before')
    @classmethod
    def iso_date(cls, value):
        if value in ('', None):
            return None
        if not isinstance(value, date) and (not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value)):
            raise ValueError('生效日期必须为 YYYY-MM-DD。')
        return value

    @model_validator(mode='after')
    def require_family(self):
        if (self.version or self.effective_from or self.scope) and not self.document_key:
            raise ValueError('填写版本、生效日期或适用范围时，请同时填写所属手册。')
        return self


class DocumentMetadataPatch(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')

    document_key: str | None = Field(default=None, max_length=100)
    version: str | None = Field(default=None, max_length=80)
    effective_from: date | None = None
    status: Literal['active', 'archived'] | None = None
    scope: str | None = Field(default=None, max_length=100)

    @field_validator('effective_from', mode='before')
    @classmethod
    def iso_date(cls, value):
        return DocumentMetadata.iso_date(value)


def select_documents(documents, document_key=None, scope=None, *, as_of=None):
    """Return eligible IDs, unresolved alternatives, and display states.

    Dates compare only inside the same explicitly named family and exact scope.
    An undated active document cannot safely be superseded by a dated document.
    """
    today = (as_of or date.today()).isoformat()
    states = {}
    groups = defaultdict(list)
    for doc in documents:
        key = doc['id']
        if doc['status'] == 'archived':
            states[key] = 'archived'
        elif doc['effective_from'] and doc['effective_from'] > today:
            states[key] = 'future'
        elif document_key is not None and doc['document_key'] != document_key:
            states[key] = 'filtered'
        elif scope is not None and doc['scope'] != scope:
            states[key] = 'filtered'
        else:
            group = (doc['document_key'], doc['scope']) if doc['document_key'] else ('', key)
            groups[group].append(doc)

    selected = []
    for group in groups.values():
        latest = max((doc['effective_from'] for doc in group if doc['effective_from']), default=None)
        for doc in group:
            if latest and doc['effective_from'] and doc['effective_from'] < latest:
                states[doc['id']] = 'superseded'
            else:
                states[doc['id']] = 'selected'
                selected.append(doc)

    families = defaultdict(list)
    for doc in selected:
        if doc['document_key']:
            families[doc['document_key']].append(doc)
    conflicts = []
    for family, docs in families.items():
        if len(docs) < 2:
            continue
        scopes = [doc['scope'] for doc in docs]
        reasons = []
        if len(set(scopes)) > 1:
            reasons.append('multiple_scopes')
        if len(set(scopes)) < len(scopes):
            reasons.append('ambiguous_versions')
        for doc in docs:
            states[doc['id']] = 'ambiguous'
        conflicts.append({'document_key': family, 'reasons': reasons, 'documents': docs})
    return {doc['id'] for doc in selected}, conflicts, states
