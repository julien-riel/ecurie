# Getting started with Harbor

Welcome to this training module. Over the next five minutes, we'll walk through how to set up your workspace in Harbor, connect your first data source, build a dashboard, and share it with your team. By the end, you'll be ready to do all of it on your own.

Harbor is a reporting tool for small teams. It pulls numbers from the systems you already use, keeps them up to date, and puts them in one place that everyone can read. You don't need to know SQL to use it, although it helps if you do.

## Setting up your workspace

Let's start with your workspace. A workspace is the shared home for your team's data, dashboards, and permissions. Most companies only ever need one.

When you first sign in, Harbor asks you for a workspace name. Choose something your colleagues will recognize, like the name of your department. Then pick your time zone. This matters more than it seems, because every scheduled report runs on that clock. If your team is spread between New York and San Francisco, choose the zone where most of your readers start their day.

Next, invite your teammates. You can add up to 25 people on the standard plan, and each one gets one of three roles. Viewers can read dashboards. Editors can build and change them. Admins can do all of that, and they also manage billing and security. Here's a good rule of thumb: start everyone as a viewer, and promote people when they ask. It's much easier to give access than to take it back.

## Connecting a data source

Now for the important part. A dashboard is only as good as the data behind it, so let's connect your first source.

Open the Sources page and click Add Source. Harbor supports more than 40 connectors, including PostgreSQL, Salesforce, GitHub, and plain CSV files. For this walkthrough, we'll use PostgreSQL, because the steps are similar for most databases.

You'll need four things from your database administrator: the host name, the port, a database name, and a read-only user. Please don't skip that last one. A read-only account means that Harbor can look at your data, but it can never change it. If something goes wrong, your production system stays safe.

Once you've entered those details, click Test Connection. Most connections succeed within about 10 seconds. If yours fails, the most common cause is a firewall. Ask your administrator to allow traffic from the two IP addresses listed on the page, and try again.

After the test passes, Harbor runs its first sync. On a typical database, this takes between 2 and 15 minutes. You can close the page while it runs. Harbor sends you an email when it's done, and from then on it refreshes your data automatically every 60 minutes.

## Building your first dashboard

With your data in place, you're ready to build something useful. Click New Dashboard, and give it a clear name. "Weekly Revenue" is a good name. "Dashboard 3" is not.

Every dashboard is made of widgets. A widget answers exactly one question, such as: how much did we sell last week? To add one, click Add Widget, choose your source, and then pick a metric and a time range. Harbor suggests a chart type for you. A line chart works well for trends over time, and a bar chart works well for comparing categories. A single large number is often the clearest choice of all.

Try to keep each dashboard to six widgets or fewer. In our own research, people stopped reading after about the seventh chart, and 68 percent of them never scrolled at all. So put the number that matters most in the top left corner, because that's where the eye lands first.

Before you move on, set a target. If your goal is $250,000 in monthly revenue, enter that as the target on your widget. Harbor then shows you, at a glance, whether you're ahead or behind, and by how much.

## Sharing your work

Finally, let's share your dashboard. Click the Share button in the top right corner. You have three options.

The first option is to add people directly. They'll see the dashboard in their Harbor home page, and their role decides what they can do with it.

The second option is a scheduled report. Harbor sends a PDF to any email address, every Monday at 9 a.m., or on whatever schedule you choose. This is the best way to reach executives, who may never log in.

The third option is a private link, which you can send to anyone at your company. Links expire after 30 days by default, and you can revoke one at any time. Are you sharing something sensitive? Then turn on the password option, and keep the expiry short.

## Wrapping up

Let's recap. You created a workspace and invited your team. You connected a data source with a read-only account. You built a focused dashboard with a clear target, and you shared it in the way that suits your readers best.

That's all you need to get started. In the next module, we'll look at alerts, so that Harbor can tell you when a number moves before anyone has to ask. Thanks for watching, and we'll see you there.
