from django.contrib.auth.models import User
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import UserProfile


@receiver(post_save, sender=User)
def ensure_user_profile(sender, instance, created, raw=False, **kwargs):
    # `raw=True` means the row is being deserialized from a fixture or a
    # migration bundle. The profile is part of that payload, so creating one
    # here would clash with the incoming UserProfile on its OneToOneField.
    if raw or not created:
        return
    UserProfile.objects.get_or_create(user=instance)
