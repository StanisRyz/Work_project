class ReadOnlyAdminMixin:
    """Keep business records visible in Admin without bypassing services."""

    def get_readonly_fields(self, request, obj=None):
        configured = super().get_readonly_fields(request, obj)
        model_fields = (
            field.name
            for field in (*self.model._meta.fields, *self.model._meta.many_to_many)
        )
        return tuple(dict.fromkeys((*configured, *model_fields)))

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
