from django.shortcuts import render


def home(request):
    return render(request, 'dashboard/home.html', {'active_page': 'dashboard'})

# Create your views here.
