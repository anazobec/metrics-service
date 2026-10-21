"""Serializers for the analytics API and its collector-specific OpenAPI shapes."""

from functools import cache

from drf_spectacular.utils import PolymorphicProxySerializer, inline_serializer
from rest_framework import serializers

from apps.analytics.models import AnalyticsPayload
from apps.analytics.payload_schemas import PayloadField, PayloadSchema
from apps.analytics.registry import CollectorEntry, enabled_collectors


class CollectorDiscoverySerializer(serializers.Serializer):
    """Serialize one enabled collector and its hyperlinked API endpoints."""

    name = serializers.CharField(read_only=True)
    description = serializers.CharField(read_only=True)
    payload_shape = serializers.ChoiceField(
        choices=("object", "array"),
        read_only=True,
        help_text="Top-level JSON shape of payload: object or array.",
    )
    key_field = serializers.CharField(
        read_only=True,
        allow_null=True,
        help_text="Field represented by top-level object keys, or null when not applicable.",
    )
    mode = serializers.CharField(read_only=True)
    accepts_since_until = serializers.BooleanField(read_only=True)
    last_collect = serializers.DateTimeField(read_only=True, allow_null=True)
    rows_url = serializers.HyperlinkedIdentityField(
        view_name="analytics:v1:rows",
        lookup_field="name",
        lookup_url_kwarg="collector",
    )
    collect_url = serializers.HyperlinkedIdentityField(
        view_name="analytics:v1:collect",
        lookup_field="name",
        lookup_url_kwarg="collector",
    )


class AnalyticsPayloadSerializer(serializers.ModelSerializer):
    """Serialise one stored raw collection envelope."""

    payload = serializers.JSONField(
        read_only=True,
        help_text="Raw collector output. The collector-specific schema is selected by collector.",
    )

    class Meta:
        model = AnalyticsPayload
        fields = ["id", "collector", "source", "cluster_id", "since", "until", "started_at", "finished_at", "payload"]
        read_only_fields = fields


def _serializer_name(value: str) -> str:
    """Convert a schema name into a valid serializer class name."""
    return "".join(part[:1].upper() + part[1:] for part in value.replace("-", "_").replace(".", "_").split("_"))


@cache
def _serializer_for_fields(name: str, fields: tuple[PayloadField, ...]) -> type[serializers.Serializer]:
    """Build a named serializer component for a registry-defined object shape."""
    attrs = {field.name: _field_to_serializer(field) for field in fields}
    attrs["__module__"] = __name__
    return type(name, (serializers.Serializer,), attrs)


def _field_to_serializer(field: PayloadField):
    """Translate a registry payload field into a DRF field for schema generation."""
    kwargs = {"required": False, "allow_null": field.nullable}
    if field.description:
        kwargs["help_text"] = field.description

    simple_fields = {
        "string": serializers.CharField,
        "integer": serializers.IntegerField,
        "number": serializers.FloatField,
        "boolean": serializers.BooleanField,
        "datetime": serializers.DateTimeField,
        "date": serializers.DateField,
    }
    if field.type in simple_fields:
        return simple_fields[field.type](**kwargs)
    if field.type == "array":
        item = field.items or PayloadField("item", "object")
        return serializers.ListField(child=_field_to_serializer(item), **kwargs)
    if field.type == "object":
        if field.fields:
            nested_name = f"{_serializer_name(field.name)}Payload"
            return _serializer_for_fields(nested_name, field.fields)(**kwargs)
        if field.additional_properties:
            return serializers.DictField(child=_field_to_serializer(field.additional_properties), **kwargs)
        return serializers.DictField(**kwargs)
    raise ValueError(f"Unsupported analytics payload field type: {field.type}")


@cache
def _payload_field(schema: PayloadSchema):
    """Build the DRF field representing one collector's complete payload."""
    if schema.kind == "array":
        item_serializer = _serializer_for_fields(schema.name, schema.item_fields)
        return serializers.ListField(child=item_serializer(), help_text=schema.description)
    if schema.kind == "object":
        if schema.additional_properties:
            return serializers.DictField(
                child=_field_to_serializer(schema.additional_properties),
                help_text=f"{schema.description} Keys are {schema.key_field} values.",
            )
        return _serializer_for_fields(schema.name, schema.fields)(help_text=schema.description)
    raise ValueError(f"Unsupported analytics payload schema kind: {schema.kind}")


@cache
def collector_payload_serializer(entry: CollectorEntry) -> type[serializers.Serializer]:
    """Build the documented collection envelope for one registry entry."""
    attrs = {
        "__module__": __name__,
        "id": serializers.IntegerField(read_only=True),
        "collector": serializers.ChoiceField(choices=[entry.name], default=entry.name),
        "source": serializers.CharField(read_only=True),
        "cluster_id": serializers.CharField(
            read_only=True,
            allow_null=True,
            help_text="Controller installation UUID (INSTALL_UUID), or null if unavailable.",
        ),
        "since": serializers.DateTimeField(read_only=True, allow_null=True),
        "until": serializers.DateTimeField(read_only=True, allow_null=True),
        "started_at": serializers.DateTimeField(read_only=True),
        "finished_at": serializers.DateTimeField(read_only=True),
        "payload": _payload_field(entry.payload_schema),
    }
    return type(f"AnalyticsPayload{_serializer_name(entry.name)}", (serializers.Serializer,), attrs)


def analytics_payload_page_serializer():
    """Return the paginated polymorphic response used by the collector rows endpoint."""
    envelopes = {entry.name: collector_payload_serializer(entry) for entry in enabled_collectors().values()}
    collector_rows = PolymorphicProxySerializer(
        component_name="AnalyticsPayloadByCollector",
        serializers=envelopes,
        resource_type_field_name="collector",
    )
    return inline_serializer(
        name="PaginatedAnalyticsPayloadList",
        fields={
            "count": serializers.IntegerField(),
            "next": serializers.URLField(allow_null=True),
            "previous": serializers.URLField(allow_null=True),
            "results": serializers.ListSerializer(child=collector_rows),
        },
    )
