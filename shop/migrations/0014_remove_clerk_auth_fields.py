from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("shop", "0013_membership_supabase_user_id_and_more")]

    operations = [
        migrations.RemoveField(model_name="membership", name="clerk_user_id"),
        migrations.RemoveField(model_name="staffinvitation", name="clerk_invitation_id"),
        migrations.RemoveField(model_name="staffinvitation", name="clerk_user_id"),
    ]
